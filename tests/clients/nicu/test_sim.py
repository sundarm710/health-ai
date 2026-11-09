"""Tests for the NICU simulation: catalog, authz, checks, mapping, submit gating, UI gating.

Unit tests use a FakeState (no network). The live test needs `make up && make sim-seed` and is skipped otherwise.
"""

import json
import re
import uuid
from datetime import datetime

import httpx
import pytest

from fhirlab.core.client import FhirClient
from fhirlab.core.client.fhir_client import BASE
from fhirlab.clients.nicu import auth
from fhirlab.clients.nicu.catalog import load_catalog, parse_fields
from fhirlab.clients.nicu.pipeline import IST, OpRequest, PayloadChanged, plan, submit
from fhirlab.clients.nicu.state import State

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=IST)
NURSE = {"sub": "N1", "role": "Nurse", "name": "Nurse Test"}
DOCTOR = {"sub": "D1", "role": "Doctor", "name": "Dr. Test"}


class FakeState(State):
    """Canned server state. Any attribute left None behaves like 'nothing found'."""

    def __init__(self, **kw):
        def boom(req):
            raise AssertionError(f"unexpected network call: {req.method} {req.url}")

        self.c = FhirClient("http://fhir.test/fhir", client=httpx.Client(transport=httpx.MockTransport(boom)))
        self.kw = kw

    def practitioners(self):
        return [{"id": "D1", "name": "Dr. Test", "role": "Doctor"}, {"id": "N1", "name": "Nurse Test", "role": "Nurse"}]

    def patient(self, pid):
        return self.kw.get("patient", {"resourceType": "Patient", "id": pid, "meta": {"versionId": "1"}})

    def encounter(self, pid):
        return self.kw.get("encounter", {"resourceType": "Encounter", "id": "E1", "meta": {"versionId": "1"}, "status": "in-progress",
                                         "period": {"start": "2026-11-07T10:00:00+05:30"}, "subject": {"reference": f"Patient/{pid}"}})

    def mother(self, pid):
        return self.kw.get("mother", {"resourceType": "RelatedPerson", "id": "M1", "meta": {"versionId": "3"}, "identifier": []})

    def possible_duplicates(self, phone, dob):
        return self.kw.get("dups", [])

    def birth_weight(self, pid):
        return self.kw.get("bw", 1.4)

    def last_weight(self, pid):
        return self.kw.get("last_weight", (1.4, "2026-11-07T10:00:00+05:30"))

    def consents(self, pid):
        return self.kw.get("consents", [])

    def devices_in_situ(self, pid):
        return self.kw.get("in_situ", [])


ADMIT = dict(
    sex="Female", baby_name="Baby of Asha Rao", birth_weight=1.4, dob="2026-11-07", tob="06:30", ga_weeks=32, ga_days=2,
    attending="D1", diagnosis="Preterm, RDS", mother_name="Asha Rao", phone="9876543210",
    abha_choice="Skip for now", abha_skip_reason="No phone linked",
)


def run(op, values, who=NURSE, *, pid="P1", idem="k1", state=None, event="2026-11-07T11:00:00+05:30"):
    req = OpRequest(op, values, pid if op != "patient.admit" else None, idem, event)
    st = state or FakeState()
    return plan(req, who, st.c, now=NOW, fhir_validate=False, state=st)


def errors(p, stage=None):
    return [i for i in p.issues if i.severity == "error" and (stage is None or i.stage == stage)]


def warnings(p):
    return [i for i in p.issues if i.severity == "warning"]


# ------------------------------------------------------------------ catalog + authz
def test_catalog_has_the_six_operations_and_the_two_spec_flags():
    cat = load_catalog()
    assert set(cat) == {"patient.admit", "consent.record", "abha.link", "anthro.record", "procedure.log", "patient.discharge"}
    assert not cat["patient.admit"].allowed("Doctor") and cat["patient.admit"].allowed("Nurse")
    assert not cat["patient.discharge"].allowed("Nurse") and cat["patient.discharge"].allowed("Doctor")
    assert all(o.allowed("Doctor") and o.allowed("Nurse") for k, o in cat.items() if k not in ("patient.admit", "patient.discharge"))


def test_authz_decides_from_token_role_and_stops_the_pipeline():
    p = run("patient.admit", ADMIT, DOCTOR)
    assert [i.stage for i in errors(p)] == ["authz"] and "patient.admit" in errors(p)[0].message
    assert p.stages["field"] == "skipped" and p.bundle is None
    assert [i.stage for i in errors(run("patient.discharge", {"outcome": "Discharged home", "summary": "ok"}, NURSE))] == ["authz"]


def test_token_signature_expiry_and_tampering():
    t = auth.sign({"sub": "N1", "role": "Nurse"})
    assert auth.verify(t)["role"] == "Nurse"
    head, body, sig = t.split(".")
    forged = auth.sign({"sub": "N1", "role": "Doctor"}).split(".")[1]
    with pytest.raises(auth.AuthError, match="bad signature"):
        auth.verify(f"{head}.{forged}.{sig}")
    with pytest.raises(auth.AuthError, match="expired"):
        auth.verify(auth.sign({"sub": "x"}, ttl=10, now=0), now=100)


# ------------------------------------------------------------------ field checks
def test_field_rules_required_range_pattern_options_and_conditionals():
    op = load_catalog()["patient.admit"]
    parsed, issues = parse_fields(op, {**ADMIT, "birth_weight": 9, "phone": "123", "ga_weeks": 50, "sex": "Robot", "abha_skip_reason": ""}, {"doctors": ["D1"]})
    msgs = {i.path: i.message for i in issues}
    assert "outside the allowed range 0.3–7" in msgs["birth_weight"]
    assert "Indian mobile" in msgs["phone"] and "22–44" in msgs["ga_weeks"] and "not one of" in msgs["sex"]
    assert msgs["abha_skip_reason"] == "Reason for skipping ABHA is required"  # unit ABHA policy = required
    assert "birth_weight" not in parsed


def test_hidden_fields_are_dropped_with_a_warning_and_unknown_fields_rejected():
    op = load_catalog()["patient.admit"]
    _, issues = parse_fields(op, {**ADMIT, "doctor_name": "Dr Who", "bogus": 1}, {"doctors": ["D1"]})
    by = {i.path: i for i in issues}
    assert by["doctor_name"].severity == "warning" and by["bogus"].severity == "error"


def test_central_line_insertion_requires_the_whole_bundle_but_removal_does_not():
    op = load_catalog()["procedure.log"]
    _, ins = parse_fields(op, {"ptype": "Central line", "action": "Insertion", "cl_device": "PICC"})
    assert len([i for i in ins if i.severity == "error"]) == 9
    _, rem = parse_fields(op, {"ptype": "Central line", "action": "Removal", "rm_reason": "End of therapy", "rm_complication": "None"})
    assert not [i for i in rem if i.severity == "error"]


# ------------------------------------------------------------------ clinical checks
def test_admit_ok_and_derived_values():
    p = run("patient.admit", ADMIT)
    assert p.ok and not p.issues
    assert p.derived["gestational_age"] == "32+2 wk" and p.derived["baby_id"].startswith("NICU-")


def test_birth_after_admission_and_weight_vs_gestation():
    p = run("patient.admit", {**ADMIT, "dob": "2026-11-08"})
    assert any("after the admission" in i.message for i in errors(p, "clinical"))
    p = run("patient.admit", {**ADMIT, "birth_weight": 3.9, "ga_weeks": 27})
    assert p.ok and any("heavy for 27 weeks" in w.message for w in warnings(p))  # a warning does not block


def test_future_event_time_is_an_error():
    assert any("future" in i.message for i in errors(run("anthro.record", {"weight": 1.4}, event="2026-11-09T09:00:00+05:30")))


def test_anthropometry_needs_at_least_one_measure():
    assert "at least one" in errors(run("anthro.record", {}), "clinical")[0].message


# ------------------------------------------------------------------ state checks
def test_no_active_admission_blocks_everything_but_admit():
    p = run("anthro.record", {"weight": 1.4}, state=FakeState(encounter=None))
    assert errors(p, "state")[0].message.startswith("This baby has no active admission")


def test_event_before_admission_is_a_state_error():
    p = run("anthro.record", {"weight": 1.4}, event="2026-11-07T08:00:00+05:30")
    assert any("before the admission" in i.message for i in errors(p, "state"))


def test_weight_plausibility_against_server_history():
    p = run("anthro.record", {"weight": 0.9}, state=FakeState(bw=1.4, last_weight=(1.4, "")))
    assert p.ok and len(warnings(p)) == 2  # >25% under birth weight, and >10% change
    assert errors(run("anthro.record", {"weight": 5.0}, state=FakeState(bw=1.4)), "state")  # >3x birth weight


def test_consent_state_machine():
    active = {"resourceType": "Consent", "id": "C1", "meta": {"versionId": "1"}, "status": "active", "category": [{"text": "Treatment & procedures"}]}
    w = {"action": "Withdraw", "scope": "Treatment & procedures"}
    assert errors(run("consent.record", w), "state")  # nothing to withdraw
    p = run("consent.record", w, state=FakeState(consents=[active]))
    assert p.ok
    entry = p.bundle["entry"][0]
    assert entry["request"] == {"method": "PUT", "url": "Consent/C1", "ifMatch": 'W/"1"'}
    assert entry["resource"]["status"] == "inactive"
    again = run("consent.record", {"action": "Record", "scope": "Treatment & procedures", "given_by": "Mother"}, state=FakeState(consents=[active]))
    assert again.ok and "already exists" in warnings(again)[0].message


def test_abha_cannot_be_silently_replaced():
    mother = {"resourceType": "RelatedPerson", "id": "M1", "meta": {"versionId": "3"},
              "identifier": [{"system": "https://healthid.abdm.gov.in", "value": "91-1111-2222-3333"}]}
    v = {"mode": "Link existing ABHA", "abha_number": "91-9999-8888-7777", "verified": "Yes"}
    assert "cannot be silently replaced" in errors(run("abha.link", v, state=FakeState(mother=mother)), "state")[0].message
    p = run("abha.link", v)
    assert p.ok and p.bundle["entry"][0]["request"]["ifMatch"] == 'W/"3"'  # read-modify-write, version-guarded


LINE = {"resourceType": "Procedure", "id": "PR1", "meta": {"versionId": "1"}, "status": "in-progress",
        "code": {"coding": [{"system": "urn:h2f:nicu-procedure", "code": "central-line-PICC"}]}, "performedPeriod": {"start": "2026-11-07T10:30:00+05:30"}}
REMOVE = {"ptype": "Central line", "action": "Removal", "cl_device": "PICC", "rm_reason": "End of therapy", "rm_complication": "None"}


def test_removal_needs_a_matching_device_in_situ_and_closes_it():
    assert "in situ" in errors(run("procedure.log", REMOVE), "state")[0].message
    p = run("procedure.log", REMOVE, state=FakeState(in_situ=[LINE]))
    assert p.ok
    reqs = [(e["request"]["method"], e["request"]["url"]) for e in p.bundle["entry"]]
    assert ("PUT", "Procedure/PR1") in reqs and ("POST", "Procedure") in reqs
    closed = next(e["resource"] for e in p.bundle["entry"] if e["request"]["method"] == "PUT")
    assert closed["status"] == "completed" and "end" in closed["performedPeriod"]


def test_discharge_closes_encounter_and_warns_about_devices():
    p = run("patient.discharge", {"outcome": "Discharged home", "summary": "Feeding well."}, DOCTOR, state=FakeState(in_situ=[LINE]))
    assert p.ok and "still in situ" in warnings(p)[0].message
    enc = next(e for e in p.bundle["entry"] if e["request"]["url"] == "Encounter/E1")
    assert enc["resource"]["status"] == "finished" and enc["request"]["ifMatch"] == 'W/"1"'


# ------------------------------------------------------------------ mapping integrity
def _refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "reference" and isinstance(v, str):
                yield v
            else:
                yield from _refs(v)
    elif isinstance(node, list):
        for x in node:
            yield from _refs(x)


def test_admit_bundle_is_atomic_idempotent_and_internally_consistent():
    b = run("patient.admit", ADMIT).bundle
    assert b["type"] == "transaction"
    kinds = [e["resource"]["resourceType"] for e in b["entry"]]
    assert kinds == ["Patient", "RelatedPerson", "Encounter", "Condition", "Observation", "Observation", "Provenance"]
    assert all("ifNoneExist" in e["request"] for e in b["entry"])  # every create is a conditional create
    full = {e["fullUrl"] for e in b["entry"]}
    assert all(r in full for r in _refs(b) if r.startswith("urn:uuid:"))  # no dangling intra-bundle references
    assert all(e["fullUrl"].startswith(("urn:", "http")) for e in b["entry"])  # PUT fullUrls must be absolute
    patient = b["entry"][0]["resource"]
    assert {i["system"] for i in patient["identifier"]} == {"http://hospital.example.org/nicu-uhid", "urn:h2f:idempotency"}
    assert patient["_birthDate"]["extension"][0]["valueDateTime"] == "2026-11-07T06:30:00+05:30"


def test_baby_id_is_stable_per_idempotency_key_and_twins_differ():
    a1, a2 = run("patient.admit", ADMIT, idem="A"), run("patient.admit", ADMIT, idem="A")
    twin = run("patient.admit", ADMIT, idem="B")
    assert a1.derived["baby_id"] == a2.derived["baby_id"] != twin.derived["baby_id"]
    assert a1.bundle == a2.bundle  # deterministic: a retry sends the identical request


def test_ambiguous_sex_is_mapped_to_other_and_the_original_is_kept():
    patient = run("patient.admit", {**ADMIT, "sex": "Ambiguous"}).bundle["entry"][0]["resource"]
    assert patient["gender"] == "other" and patient["extension"][0]["valueString"] == "Ambiguous"


def test_provenance_names_the_signed_in_actor():
    prov = run("anthro.record", {"weight": 1.4}, DOCTOR).bundle["entry"][-1]["resource"]
    assert prov["agent"][0]["who"]["reference"] == "Practitioner/D1" and prov["agent"][0]["type"]["text"] == "Doctor"


# ------------------------------------------------------------------ validate -> submit gating
def test_payload_hash_changes_with_any_input():
    base = run("anthro.record", {"weight": 1.4}).payload_hash
    assert base == run("anthro.record", {"weight": 1.4}).payload_hash
    assert base != run("anthro.record", {"weight": 1.5}).payload_hash
    assert base != run("anthro.record", {"weight": 1.4}, idem="other").payload_hash
    assert base != run("anthro.record", {"weight": 1.4}, DOCTOR).payload_hash


def test_submit_refuses_a_payload_that_changed_after_validation():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": [{"response": {"status": "201 Created", "location": "Observation/1"}}]},
                              headers={"content-type": "application/fhir+json"})

    client = FhirClient("http://fhir.test/fhir", client=httpx.Client(transport=httpx.MockTransport(handler)))
    req = OpRequest("anthro.record", {"weight": 1.4}, "P1", "k1", "2026-11-07T11:00:00+05:30")
    validated = plan(req, NURSE, client, now=NOW, fhir_validate=False, state=FakeState()).payload_hash
    tampered = OpRequest("anthro.record", {"weight": 1.5}, "P1", "k1", "2026-11-07T11:00:00+05:30")
    with pytest.raises(PayloadChanged):
        submit(tampered, NURSE, client, validated, now=NOW, fhir_validate=False, state=FakeState())
    assert not seen  # nothing was sent
    _, results = submit(req, NURSE, client, validated, now=NOW, fhir_validate=False, state=FakeState())
    assert results == [{"status": "201 Created", "location": "Observation/1"}] and len(seen) == 1
    assert json.loads(seen[0].content)["type"] == "transaction"


def test_submit_does_not_send_when_the_plan_has_errors():
    sent = []
    client = FhirClient("http://fhir.test/fhir", client=httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r))))
    req = OpRequest("anthro.record", {}, "P1", "k1", "2026-11-07T11:00:00+05:30")
    h = plan(req, NURSE, client, now=NOW, fhir_validate=False, state=FakeState()).payload_hash
    p, results = submit(req, NURSE, client, h, now=NOW, fhir_validate=False, state=FakeState())
    assert results is None and not p.ok and not sent


# ------------------------------------------------------------------ UI gating (Streamlit AppTest, fake middleware)
def _ui_script():
    from fhirlab.console.pages import client as ui
    from fhirlab.console.pages.client import client_page

    def fake_call(self, method, path, **kw):
        from dataclasses import asdict as _asdict

        import streamlit as _st

        _st.session_state.setdefault("_calls", []).append(f"{method} {path}")

        from fhirlab.clients.nicu.catalog import load_catalog as _lc

        if path == "/catalog":
            ops = {}
            for o in _lc().values():
                d = _asdict(o)
                d["fields"] = [{**f, "options": list(f["options"])} for f in d["fields"]]
                ops[o.id] = d
            return 200, {"operations": ops, "dynamic_options": {"doctors": [{"id": "D1", "name": "Dr. Test"}]}}
        if path == "/practitioners":
            return 200, [{"id": "N1", "name": "Nurse Test", "role": "Nurse"}, {"id": "D1", "name": "Dr. Test", "role": "Doctor"}]
        if path == "/patients":
            return 200, [{"id": "P1", "name": "Baby A", "since": "x"}]
        if path == "/login":
            return 200, {"token": "aaa.eyJzdWIiOiJOMSIsInJvbGUiOiJOdXJzZSJ9.sig"}
        if path == "/validate":
            w = kw["json"]["values"].get("weight")
            ok = w is not None and w >= 1
            return 200, {"ok": ok, "payload_hash": "h", "stages": {"authz": "ok", "field": "ok" if ok else "1 error(s)"},
                         "issues": [] if ok else [{"severity": "error", "stage": "field", "path": "weight", "message": "weight must be at least 1 kg"}]}
        return 404, {}

    ui.Api.call = fake_call
    import streamlit as st

    st.set_page_config(layout="wide")
    client_page()


def test_ui_submit_is_gated_on_a_current_passing_validation():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_ui_script, default_timeout=60).run()
    assert not at.exception
    at.selectbox(key="op").set_value("anthro.record").run()
    submit_btn = lambda: next(b for b in at.button if b.label == "Submit")  # noqa: E731
    validate_btn = lambda: next(b for b in at.button if b.label == "Validate")  # noqa: E731
    assert submit_btn().disabled
    at.number_input(key="anthro.record:weight").set_value(0.5).run()  # the fake middleware rejects < 1 kg
    at = validate_btn().click().run()
    assert submit_btn().disabled and any("at least 1 kg" in e.value for e in at.error)  # failed validation keeps Submit off
    at.number_input(key="anthro.record:weight").set_value(1.4).run()
    assert submit_btn().disabled  # edited but not re-validated
    at = validate_btn().click().run()
    assert not submit_btn().disabled  # passed, unchanged
    at.number_input(key="anthro.record:weight").set_value(1.5).run()
    assert submit_btn().disabled  # editing after a pass switches Submit off again


# ------------------------------------------------------------------ live (needs `make up` + `make sim-seed`)
def _live() -> bool:
    try:
        return httpx.get(f"{BASE}/PractitionerRole?_count=1", timeout=2).json().get("total", 0) > 0
    except (httpx.HTTPError, ValueError):
        return False


@pytest.mark.skipif(not _live(), reason="HAPI not running or not seeded (make up && make sim-seed)")
def test_live_admit_is_atomic_and_a_retry_creates_nothing_new(live_cleanup):
    from fastapi.testclient import TestClient

    from fhirlab.clients.nicu.api import app

    c = TestClient(app)
    pr = {p["name"]: p for p in c.get("/practitioners").json()}
    nurse = pr["Nurse Kavya Nair"]
    tok = c.post("/login", json={"practitioner_id": nurse["id"], "role": "Nurse"}).json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    assert c.post("/login", json={"practitioner_id": nurse["id"], "role": "Doctor"}).status_code == 403  # role spoof refused
    doctor = next(p for p in pr.values() if p["role"] == "Doctor")
    body = {"op": "patient.admit", "idempotency_key": str(uuid.uuid4()), "event_time": datetime.now(IST).isoformat(),
            "values": {**ADMIT, "dob": datetime.now(IST).date().isoformat(), "tob": "00:01", "attending": doctor["id"], "phone": "9000000001"}}
    v = c.post("/validate", json=body, headers=H).json()
    assert v["ok"], v["issues"]
    first = c.post("/submit", json={**body, "validated_hash": v["payload_hash"]}, headers=H).json()
    for r in first.get("results", []):  # register for deletion before any assertion can fail
        if m := re.search(r"(?:^|/)Patient/([^/]+)", r["location"]):  # relative ("Patient/1/_history/1") or absolute URL
            live_cleanup(m.group(1))
    assert first["submitted"] and all(r["status"].startswith("201") for r in first["results"])
    retry = c.post("/submit", json={**body, "validated_hash": v["payload_hash"]}, headers=H).json()
    assert retry["submitted"] and all(r["status"].startswith("200") for r in retry["results"])
    assert [r["location"] for r in first["results"]] == [r["location"] for r in retry["results"]]  # same resources, no duplicates
    assert c.post("/submit", json={**body, "values": {**body["values"], "birth_weight": 1.5}, "validated_hash": v["payload_hash"]}, headers=H).status_code == 409



def test_ui_required_marker_follows_the_servers_rules():
    from fhirlab.console.pages.client import is_required

    f = {"required": False, "required_if": None, "required_if_policy": "abha_required"}
    assert is_required(f, {}, {"abha_required": True}) and not is_required(f, {}, {"abha_required": False}) and not is_required(f, {}, {})
    g = {"required": False, "required_if": {"x": "y"}, "required_if_policy": None}
    assert is_required(g, {"x": "y"}, {}) and not is_required(g, {"x": "n"}, {})  # conditional: only starred while the condition holds
    assert is_required({"required": True}, {}, {})


def test_ui_typing_in_the_form_does_not_call_the_middleware():
    """Streamlit reruns the page on every interaction. Reference data (catalog, practitioners, patients) must be cached,
    so editing a field costs zero calls and Validate costs exactly one (the validation itself)."""
    import uuid as _uuid

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_ui_script, default_timeout=60)
    at.session_state["api_base"] = f"http://fake.test/{_uuid.uuid4().hex}"  # unique cache key: no leakage between tests
    at.run()
    calls = lambda: list(at.session_state["_calls"])  # noqa: E731
    first = calls()
    assert sorted(c for c in first if c.startswith("GET")) == ["GET /catalog", "GET /patients", "GET /practitioners"]

    at.selectbox(key="op").set_value("anthro.record").run()
    at.number_input(key="anthro.record:weight").set_value(1.3).run()
    at.number_input(key="anthro.record:weight").set_value(1.4).run()
    assert calls() == first, f"form interactions called the middleware: {calls()[len(first):]}"

    n = len(calls())
    next(b for b in at.button if b.label == "Validate").click().run()
    assert calls()[n:] == ["POST /validate"]  # one validation: no reference refetch, no second full run

    n = len(calls())
    next(b for b in at.button if b.label == "Refresh").click().run()
    assert sorted(calls()[n:]) == ["GET /catalog", "GET /patients", "GET /practitioners"]  # Refresh really reloads
