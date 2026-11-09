"""Headless smoke tests for the Server and Database pages (Streamlit AppTest) against fake adapters."""

import json

import pandas as pd
from streamlit.testing.v1 import AppTest

from fhirlab.obs import JsonlSink
from fhirlab.obs.record import RequestLog, Upstream


def _server_script(path):
    from fhirlab.obs import JsonlLogStore
    from fhirlab.ui.server import server_page

    server_page(JsonlLogStore(path), middleware_url="http://x", hapi_url="http://y")


def test_server_page_renders_empty_then_with_data(tmp_path):
    p = tmp_path / "l.jsonl"
    at = AppTest.from_function(_server_script, args=(str(p),), default_timeout=60).run()
    assert not at.exception and any("No requests logged" in i.value for i in at.info)
    sink = JsonlSink(p)
    now = pd.Timestamp.now(tz="UTC")
    for i, (st_, cat) in enumerate([(200, "ok"), (409, "conflict"), (502, "upstream_error")]):
        sink(RequestLog(ts=(now - pd.Timedelta(seconds=30 * i)).isoformat(), request_id=f"r{i}", method="POST", route="/submit", status=st_, ms=40.0 + i,
                        category=cat, upstream=[Upstream("transaction", "-", "POST", st_, 30.0, 5.0)]))
    at = AppTest.from_function(_server_script, args=(str(p),), default_timeout=60).run()
    assert not at.exception
    assert [m.label for m in at.metric][:3] == ["Requests", "Throughput", "Server errors"] and at.metric[0].value == "3"
    assert any("Latency by route" in m.value for m in at.markdown)  # per-route distribution section is on the Pulse tab


def _db_script():
    from fhirlab.dbview.repo import PgRepo
    from fhirlab.ui.database import database_page

    database_page(PgRepo())


def test_database_page_renders_against_live_postgres_or_shows_friendly_error():
    at = AppTest.from_function(_db_script, default_timeout=120).run()
    assert not at.exception
    assert at.error or at.tabs  # either "Cannot reach Postgres" or the three lenses
