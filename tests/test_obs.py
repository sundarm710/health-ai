"""Server observability: middleware -> JSONL -> analytics. No network, no Streamlit."""

import json

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from h2f.client import FhirClient
from h2f.obs import JsonlLogStore, JsonlSink, RequestLogMiddleware, annotate
from h2f.obs import analytics as an
from h2f.obs.middleware import current_request_id, record_upstream
from h2f.obs.record import categorize, classify_upstream

BASE = "http://fhir.test/fhir"


def _app(tmp_path):
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware, sink=JsonlSink(tmp_path / "l.jsonl"))
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json={"resourceType": "Patient", "id": "9"}))

    @app.get("/patients/{pid}")
    def get(pid: str):
        annotate(op="patient.read", actor="D1", role="Doctor")
        FhirClient(BASE, client=httpx.Client(transport=handler), on_call=lambda c: record_upstream(c, BASE)).read("Patient", pid)
        return {"rid": current_request_id()}

    @app.get("/teapot")
    def teapot():
        raise HTTPException(409, "stale")

    @app.get("/boom")
    def boom():
        raise RuntimeError("x")

    @app.get("/health")
    def health():
        return {}

    return app, JsonlLogStore(tmp_path / "l.jsonl")


def test_one_structured_line_per_request_with_route_template_upstream_and_request_id(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/patients/SECRET-ID-123")
    c.get("/teapot")
    c.get("/boom")
    c.get("/health")  # skipped
    c.get("/nope")
    logs = store.read()
    assert [x.status for x in logs] == [200, 409, 500, 404]
    ok = logs[0]
    assert ok.route == "/patients/{pid}" and "SECRET" not in json.dumps(ok.to_dict())  # template, never the raw id
    assert ok.request_id == r.headers["x-request-id"] == r.json()["rid"] and len(ok.request_id) == 16
    assert (ok.op, ok.actor, ok.role) == ("patient.read", "D1", "Doctor")
    assert [(u.interaction, u.resource, u.status) for u in ok.upstream] == [("read", "Patient", 200)]
    assert [x.category for x in logs] == ["ok", "conflict", "server_error", "client_error"]
    assert logs[2].error == "RuntimeError" and logs[3].route == "(unmatched)"


def test_incoming_request_id_is_honoured_and_garbage_is_replaced(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    assert c.get("/teapot", headers={"X-Request-ID": "abc123"}).headers["x-request-id"] == "abc123"
    assert c.get("/teapot", headers={"X-Request-ID": "not valid!!"}).headers["x-request-id"] != "not valid!!"


def test_categorize_and_classify():
    assert [categorize(s) for s in (200, 201, 401, 403, 409, 412, 422, 400, 404, 502, 500)] == [
        "ok", "ok", "auth", "auth", "conflict", "conflict", "validation", "client_error", "client_error", "upstream_error", "server_error"]
    assert classify_upstream("GET", f"{BASE}/Patient/1", BASE) == ("read", "Patient")
    assert classify_upstream("GET", f"{BASE}/Observation?subject=Patient/1", BASE) == ("search", "Observation")
    assert classify_upstream("POST", BASE, BASE) == ("transaction", "-")
    assert classify_upstream("POST", f"{BASE}/Patient/$validate", BASE) == ("validate", "Patient")
    assert classify_upstream("GET", f"{BASE}/metadata", BASE) == ("metadata", "-")


def test_analytics_view_models(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app, raise_server_exceptions=False)
    for _ in range(5):
        c.get("/patients/1")
    c.get("/teapot")
    c.get("/boom")
    logs = store.read()
    df = an.to_frame(logs)
    k = an.kpis(df)
    assert k["requests"] == 7 and k["error_rate"] == round(1 / 7 * 100, 2) and k["client_error_rate"] == round(1 / 7 * 100, 2)
    assert k["p50"] <= k["p95"] <= k["p99"]
    assert set(an.by_category(df)["category"]) == set(an.CATEGORIES)
    assert an.by_route(df).set_index("route").loc["/patients/{pid}", "requests"] == 5
    assert an.upstream_summary(an.upstream_frame(logs)).iloc[0].calls == 5
    assert (df["self_ms"] >= 0).all() and an.histogram(df)["requests"].sum() == 7
    wf = an.waterfall(logs[0])
    assert list(wf["kind"]) == ["request", "upstream"] and wf.loc[1, "end"] <= wf.loc[0, "end"] + 1
    assert len(an.timeseries(df, "1min")) >= 1
    assert an.filter_frame(df, categories=["conflict"]).shape[0] == 1
    assert an.compare_windows(df, pd.Timedelta("1h"), pd.Timestamp.now(tz="UTC"))[0]["requests"] == 7


def test_garbled_line_does_not_hide_the_rest(tmp_path):
    app, store = _app(tmp_path)
    TestClient(app).get("/teapot")
    with (tmp_path / "l.jsonl").open("a") as f:
        f.write("{torn\n")
    TestClient(app).get("/teapot")
    assert len(store.read()) == 2


def test_empty_store_gives_empty_but_well_formed_frames(tmp_path):
    df = an.to_frame([])
    assert an.kpis(df)["requests"] == 0 and an.by_route(df).empty and an.timeseries(df).empty and an.histogram(df).empty


def test_route_distribution_top_n_shared_edges_and_percentiles():
    ts = pd.Timestamp("2026-11-09T10:00:00Z")
    rows = ([("GET", "/a", ms) for ms in range(1, 101)]  # busiest: 1..100 ms
            + [("POST", "/b", ms) for ms in (10, 20, 30)] + [("GET", "/c", 500)])
    df = pd.DataFrame([{"ts": ts, "method": m, "route": r, "ms": float(ms)} for m, r, ms in rows])
    b, s = an.route_distribution(df, top=2, bins=10)
    assert list(s["route"]) == ["GET /a", "POST /b"]  # top 2 by count, busiest first; /c dropped
    assert s.set_index("route").loc["GET /a", "p50"] == 50.5 and s.set_index("route").loc["GET /a", "p95"] == 95.0  # rounded to 0.1 ms
    per = b.groupby("route")
    assert per["requests"].sum().to_dict() == {"GET /a": 100, "POST /b": 3}  # every request lands in a bucket
    assert per["share"].sum().round(6).eq(1).all()
    edges = lambda r: b[b["route"] == r][["lo", "hi"]].reset_index(drop=True)  # noqa: E731
    assert edges("GET /a").equals(edges("POST /b"))  # same edges -> rows align
    e_b, e_s = an.route_distribution(pd.DataFrame(columns=an.COLS))
    assert e_b.empty and e_s.empty
