"""The middleware as an HTTP service.   `make sim-api`  ->  http://localhost:8000/docs

    POST /login      practitioner + role  -> signed token (role verified against PractitionerRole in HAPI)
    GET  /catalog    operations, fields, permissions (dynamic options resolved from HAPI)
    GET  /patients   babies with an active admission
    POST /validate   dry run: parse, check, map. Returns issues + the exact FHIR call. Writes nothing.
    POST /submit     re-validates, requires the validated payload hash, then POSTs the transaction.

Every response carries `trace`: the requests this service made to HAPI while handling the call.
"""

from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from h2f.client import FhirClient, FhirError
from h2f.obs import JsonlLogStore, JsonlSink, RequestLogMiddleware, annotate
from h2f.obs.api import make_router
from h2f.obs.middleware import current_request_id, record_upstream
from h2f.sim import auth
from h2f.sim.catalog import ROLES, UNIT_POLICY, load_catalog
from h2f.sim.pipeline import OpRequest, PayloadChanged, plan, submit
from h2f.sim.state import State

app = FastAPI(title="NICU FHIR simulation middleware")
HAPI = os.environ.get("HAPI_BASE", "http://localhost:8080/fhir")

# Observability: one structured log line per request (see h2f/obs). The log store is also exposed read-only at /ops/*.
app.add_middleware(RequestLogMiddleware, sink=JsonlSink())
app.include_router(make_router(JsonlLogStore()))


class LoginBody(BaseModel):
    practitioner_id: str
    role: str


class OpBody(BaseModel):
    op: str
    values: dict = Field(default_factory=dict)
    patient_id: str | None = None
    idempotency_key: str = ""
    event_time: str | None = None
    validated_hash: str | None = None  # /submit only

    def request(self) -> OpRequest:
        return OpRequest(self.op, self.values, self.patient_id, self.idempotency_key, self.event_time)


def _client() -> tuple[FhirClient, list[dict]]:
    trace: list[dict] = []

    def on_call(call: dict) -> None:
        trace.append(call)  # for the response's `trace` (UI trace panel)
        record_upstream(call, HAPI)  # for the server's own request log

    # Forward our request id: HAPI stores it in hfj_res_ver.request_id, tying a DB row back to the log line.
    rid = current_request_id()
    return FhirClient(HAPI, on_call=on_call, headers={"X-Request-ID": rid} if rid else None), trace


def claims(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    try:
        who = auth.verify(authorization.removeprefix("Bearer "))
        annotate(actor=who.get("sub"), role=who.get("role"))
        return who
    except auth.AuthError as e:
        raise HTTPException(401, str(e)) from None


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/practitioners")
def practitioners() -> list[dict]:
    return State(_client()[0]).practitioners()


@app.post("/login")
def login(b: LoginBody) -> dict:
    if b.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    st = State(_client()[0])
    actual = st.practitioner_role(b.practitioner_id)
    annotate(actor=b.practitioner_id, role=b.role, outcome="role_mismatch" if actual != b.role else "signed_in")
    if actual != b.role:
        raise HTTPException(403, f"Practitioner/{b.practitioner_id} is registered as {actual or 'unknown'}, not {b.role}")
    name = next((p["name"] for p in st.practitioners() if p["id"] == b.practitioner_id), "")
    return {"token": auth.sign({"sub": b.practitioner_id, "role": b.role, "name": name}), "name": name, "role": b.role}


@app.get("/catalog")
def catalog() -> dict:
    st = State(_client()[0])
    doctors = [{"id": p["id"], "name": p["name"]} for p in st.practitioners() if p["role"] == "Doctor"]
    ops = {}
    for op in load_catalog().values():
        d = asdict(op)
        d["fields"] = [{**f, "options": list(f["options"])} for f in d["fields"]]
        ops[op.id] = d
    return {"operations": ops, "dynamic_options": {"doctors": doctors}, "unit_policy": UNIT_POLICY}


@app.get("/patients")
def patients() -> list[dict]:
    return State(_client()[0]).active_babies()


@app.post("/validate")
def validate(b: OpBody, who: dict = Depends(claims)) -> dict:
    c, trace = _client()
    p = plan(b.request(), who, c)
    annotate(op=b.op, outcome="valid" if p.ok else "invalid")
    return {**p.as_dict(), "trace": trace}


@app.post("/submit")
def submit_op(b: OpBody, who: dict = Depends(claims)) -> JSONResponse:
    annotate(op=b.op)
    if not b.validated_hash:
        raise HTTPException(400, "validated_hash is required: call /validate first")
    c, trace = _client()
    try:
        p, results = submit(b.request(), who, c, b.validated_hash)
    except PayloadChanged as e:
        annotate(outcome="payload_changed")
        return JSONResponse({"submitted": False, "error": str(e), "trace": trace}, status_code=409)
    except FhirError as e:
        annotate(outcome=f"hapi_{e.status}")
        # e.g. HAPI 409 because someone else updated a resource after we read it
        return JSONResponse(
            {"submitted": False, "error": f"HAPI rejected the transaction: {e}", "hapi_status": e.status, "trace": trace},
            status_code=409 if e.status in (409, 412) else 502,
        )
    annotate(outcome="blocked" if results is None else "submitted")
    if results is None:
        return JSONResponse({"submitted": False, **p.as_dict(), "trace": trace}, status_code=422)
    return JSONResponse({"submitted": True, **p.as_dict(), "results": results, "trace": trace})
