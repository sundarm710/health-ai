"""Unit tests use httpx.MockTransport (no server). Integration tests run against a live HAPI
(`make up`) and are skipped automatically when it isn't reachable."""

import httpx
import pytest

from fhirlab.client import FhirClient, FhirError
from fhirlab.client.fhir_client import BASE
from fhirlab.fhir import MRN_SYSTEM


def _client(handler) -> FhirClient:
    return FhirClient("http://fhir.test/fhir", client=httpx.Client(transport=httpx.MockTransport(handler)))


def _json(body, status=200, **headers):
    return httpx.Response(status, json=body, headers={"content-type": "application/fhir+json", **headers})


def test_conditional_create_sends_if_none_exist():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(method=req.method, url=str(req.url), cond=req.headers.get("if-none-exist"))
        return _json({"resourceType": "Patient", "id": "7", "meta": {"versionId": "1"}}, 201,
                     Location="http://fhir.test/fhir/Patient/7/_history/1", ETag='W/"1"')

    r = _client(handler).create_patient("Rao", "Asha", "1988-04-12", "female", mrn="M1")
    assert (r.status, r.id, r.version, r.etag) == (201, "7", "1", 'W/"1"')
    assert seen["method"] == "POST" and seen["url"] == "http://fhir.test/fhir/Patient"
    assert seen["cond"] == f"identifier={MRN_SYSTEM}|M1"


def test_observation_references_patient_and_uses_loinc_unit():
    sent = {}

    def handler(req: httpx.Request):
        import json
        sent.update(json.loads(req.content))
        return _json({**sent, "id": "o1"}, 201)

    _client(handler).add_observation("7", "29463-7", 61.5)
    assert sent["subject"] == {"reference": "Patient/7"}
    assert sent["code"]["coding"][0]["code"] == "29463-7"
    assert sent["valueQuantity"] == {"value": 61.5, "unit": "kg", "system": "http://unitsofmeasure.org", "code": "kg"}


def test_search_follows_next_links_and_skips_outcomes():
    def handler(req: httpx.Request):
        if "page2" in str(req.url):
            return _json({"resourceType": "Bundle", "entry": [{"resource": {"id": "c"}, "search": {"mode": "match"}}]})
        return _json({
            "resourceType": "Bundle",
            "link": [{"relation": "next", "url": "http://fhir.test/fhir?page2"}],
            "entry": [{"resource": {"id": "a"}}, {"resource": {"id": "b"}},
                      {"resource": {"resourceType": "OperationOutcome"}, "search": {"mode": "outcome"}}],
        })

    assert [r["id"] for r in _client(handler).search("Patient")] == ["a", "b", "c"]


def test_error_surfaces_operation_outcome():
    oo = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "diagnostics": "bad gender"}]}
    with pytest.raises(FhirError) as e:
        _client(lambda req: _json(oo, 422)).read("Patient", "x")
    assert e.value.status == 422 and "bad gender" in str(e.value)


# ---------------------------------------------------------------- integration (live HAPI + Postgres)
def _live() -> bool:
    try:
        return httpx.get(f"{BASE}/metadata", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


live = pytest.mark.skipif(not _live(), reason="HAPI not running (make up)")


@live
def test_round_trip_idempotent_create_versions_and_optimistic_locking():
    import uuid
    c = FhirClient()
    mrn = f"TEST-{uuid.uuid4().hex[:8]}"
    first = c.create_patient("Test", "Pat", "2000-01-01", "other", mrn=mrn)
    again = c.create_patient("Test", "Pat", "2000-01-01", "other", mrn=mrn)
    assert (first.status, again.status) == (201, 200) and first.id == again.id

    c.add_observation(first.id, "8867-4", 70, effective="2026-01-01T00:00:00Z")
    assert [o["valueQuantity"]["value"] for o in c.patient_observations(first.id)] == [70]

    got = c.read("Patient", first.id)
    p = got.resource | {"gender": "female"}
    assert c.update(p, if_match=got.etag).version == "2"
    with pytest.raises(FhirError) as e:  # stale ETag
        c.update(p | {"gender": "male"}, if_match=got.etag)
    assert e.value.status == 409  # spec says 412; HAPI answers 409 Conflict
    assert len(c.history("Patient", first.id)) == 2
    assert {r["resourceType"] for r in c.everything(first.id)} == {"Patient", "Observation"}
