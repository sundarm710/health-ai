import os
import tempfile

# Tests that import the real FastAPI app must never write into the developer's real request log.
os.environ.setdefault("FHIRLAB_LOG_PATH", os.path.join(tempfile.mkdtemp(prefix="fhirlab-test-logs-"), "middleware.jsonl"))


import pytest  # noqa: E402


@pytest.fixture
def live_cleanup():
    """Live tests write to the REAL local HAPI. Call `live_cleanup(patient_id)` right after creating a patient;
    on teardown the patient and everything in its compartment are deleted, so test runs never leave babies in the NICU list.

    HAPI enforces referential integrity, so a referenced resource cannot be deleted first: delete whatever it will
    allow, repeat until nothing is left (Provenance/Observation before Encounter before Patient, without hard-coding the order).
    """
    from fhirlab.core.client import FhirClient, FhirError

    # Only patient-owned types. Shared reference data ($everything also returns it: Practitioner, Organization...) must never be deleted.
    owned = {"Patient", "Encounter", "RelatedPerson", "Observation", "Condition", "Procedure", "Consent", "Provenance", "DiagnosticReport"}
    ids: list[str] = []
    yield ids.append
    c = FhirClient()
    for pid in ids:
        left = {(r["resourceType"], r["id"]) for r in c.everything(pid) if r["resourceType"] in owned}
        left |= {("Provenance", r["id"]) for r in c.search("Provenance", {"target": f"Patient/{pid}"}, headers={"Cache-Control": "no-cache"})}
        for _ in range(6):
            for rt, rid in sorted(left):
                try:
                    c.delete(rt, rid)
                    left.discard((rt, rid))
                except FhirError as e:
                    if e.status not in (404, 409, 410):
                        raise
                    if e.status != 409:
                        left.discard((rt, rid))
            if not left:
                break
        assert not left, f"live_cleanup could not delete: {sorted(left)}"
