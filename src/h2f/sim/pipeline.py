"""The middleware pipeline: authz -> parse -> clinical -> state -> map -> HAPI $validate -> plan.

`plan()` is a dry run (nothing is written). `submit()` re-runs the whole pipeline, insists the payload
is byte-for-byte what was validated, and only then POSTs the transaction. Re-running matters: the
server's state may have changed since the user pressed Validate (time-of-check/time-of-use).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from h2f.client import FhirClient
from h2f.sim.catalog import Issue, Operation, load_catalog, parse_fields
from h2f.sim.checks import Ctx, clinical, server_state
from h2f.sim.mapping import map_operation
from h2f.sim.state import State

IST = ZoneInfo("Asia/Kolkata")


class PayloadChanged(Exception):
    """Submit was attempted with a payload that differs from the one that was validated."""


@dataclass
class OpRequest:
    op: str
    values: dict
    patient_id: str | None = None
    idempotency_key: str = ""
    event_time: str | None = None  # ISO 8601; defaults to now (IST)


@dataclass
class Plan:
    ok: bool
    issues: list[Issue] = field(default_factory=list)
    parsed: dict = field(default_factory=dict)
    derived: dict = field(default_factory=dict)
    bundle: dict | None = None
    requests: list[dict] = field(default_factory=list)
    payload_hash: str = ""
    stages: dict = field(default_factory=dict)  # stage -> "ok" | "N errors" | "skipped"

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "issues": [i.as_dict() for i in self.issues], "parsed": self.parsed,
            "derived": self.derived, "bundle": self.bundle, "requests": self.requests,
            "payload_hash": self.payload_hash, "stages": self.stages,
        }


def payload_hash(req: OpRequest, claims: dict) -> str:
    body = json.dumps(
        {"op": req.op, "values": req.values, "patient": req.patient_id, "idem": req.idempotency_key,
         "event_time": req.event_time, "actor": claims.get("sub")},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _summarise(bundle: dict) -> list[dict]:
    out = []
    for e in bundle["entry"]:
        r = e["request"]
        out.append({k: v for k, v in {
            "method": r["method"], "url": r["url"], "ifNoneExist": r.get("ifNoneExist"),
            "ifMatch": r.get("ifMatch"), "resource": e["resource"]["resourceType"],
        }.items() if v})
    return out


def plan(
    req: OpRequest, claims: dict, client: FhirClient, *, now: datetime | None = None,
    fhir_validate: bool = True, state: State | None = None,
) -> Plan:
    now = now or datetime.now(IST)
    p = Plan(ok=False, payload_hash=payload_hash(req, claims))
    ops = load_catalog()
    if req.op not in ops:
        p.issues.append(Issue("error", "field", "op", f"Unknown operation '{req.op}'"))
        return p
    op: Operation = ops[req.op]

    # 1. authz: decided from the signed token, never from anything the form sent
    if not op.allowed(claims["role"]):
        why = f" ({op.flag})" if op.flag else ""
        p.issues.append(Issue("error", "authz", "role", f"{claims['role']} ({claims['name']}) may not perform {op.id}{why}"))
        p.stages = {"authz": "1 error", "field": "skipped", "clinical": "skipped", "state": "skipped", "fhir": "skipped"}
        return p
    p.stages["authz"] = "ok"

    try:
        event_time = datetime.fromisoformat(req.event_time) if req.event_time else now
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=IST)
    except ValueError:
        p.issues.append(Issue("error", "field", "event_time", f"'{req.event_time}' is not an ISO 8601 date-time"))
        return p

    state = state or State(client)
    ctx = Ctx(now=now, event_time=event_time, actor=claims, state=state, patient_id=req.patient_id, idem=req.idempotency_key)

    # 2. parse + field checks
    dynamic, doctors = {}, {}
    if any(f.options_from == "doctors" for f in op.fields):
        doctors = {x["id"]: x for x in state.practitioners() if x["role"] == "Doctor"}
        dynamic["doctors"] = list(doctors)
    parsed, issues = parse_fields(op, req.values, dynamic)
    if not req.idempotency_key:
        issues.append(Issue("error", "field", "idempotency_key", "Missing idempotency key (a retry could create duplicates)"))
    p.issues += issues
    nerr = sum(i.severity == "error" for i in issues)
    p.parsed = parsed
    p.stages["field"] = "ok" if not nerr else f"{nerr} error(s)"
    if nerr:
        p.stages |= {"clinical": "skipped", "state": "skipped", "fhir": "skipped"}
        return p
    if parsed.get("attending") in doctors:
        ctx.scratch["attending"] = doctors[parsed["attending"]]

    # 3. clinical + 4. state
    for stage, fn in (("clinical", clinical), ("state", server_state)):
        found = fn(req.op, parsed, ctx)
        p.issues += found
        n = sum(i.severity == "error" for i in found)
        p.stages[stage] = "ok" if not n else f"{n} error(s)"
        if n:
            for s in ("clinical", "state", "fhir"):
                p.stages.setdefault(s, "skipped")
            p.stages["fhir"] = "skipped"
            return p

    # 5. map, then structural validation by HAPI itself
    p.bundle, p.derived = map_operation(req.op, parsed, ctx)
    p.requests = _summarise(p.bundle)
    if fhir_validate:
        bad = [i for i in client.validate(p.bundle) if i.get("severity") in ("error", "fatal")]
        for i in bad:
            loc = (i.get("expression") or i.get("location") or [""])[0]
            p.issues.append(Issue("error", "fhir", loc, i.get("diagnostics", "FHIR validation error")))
        p.stages["fhir"] = "ok" if not bad else f"{len(bad)} error(s)"
        if bad:
            return p
    else:
        p.stages["fhir"] = "not run"
    p.ok = True
    return p


def submit(req: OpRequest, claims: dict, client: FhirClient, validated_hash: str, **kw) -> tuple[Plan, list[dict] | None]:
    """Re-plan, require the same payload that was validated, then send the transaction."""
    p = plan(req, claims, client, **kw)
    if p.payload_hash != validated_hash:
        raise PayloadChanged("The form changed after it was validated; validate again.")
    if not p.ok:
        return p, None
    resp = client.transaction(p.bundle).resource
    results = [
        {"status": e.get("response", {}).get("status", ""), "location": e.get("response", {}).get("location", "")}
        for e in resp.get("entry", [])
    ]
    return p, results
