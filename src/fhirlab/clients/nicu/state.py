"""Server-state lookups the middleware needs for state/reference checks.

Everything goes through FhirClient, so each lookup shows up in the trace panel as a real
middleware -> HAPI request. Tests subclass State with canned answers.
"""

from __future__ import annotations

from fhirlab.core.client import FhirClient, FhirError
from fhirlab.core.fhir import LOINC

ROLE_SYSTEM = "http://hospital.example.org/practitioner-role"
NICU_UHID = "http://hospital.example.org/nicu-uhid"
IDEM = "urn:h2f:idempotency"
MOTHER_REL = ("http://terminology.hl7.org/CodeSystem/v3-RoleCode", "MTH")
LOINC_BIRTH_WEIGHT = "8339-4"
LOINC_WEIGHT = "29463-7"


def _ref_id(ref: str | None) -> str | None:
    return ref.split("/")[-1] if ref else None


# HAPI caches identical searches for 60 s by default. A validation check that reads a stale answer
# ("no line in situ" right after one was inserted) is a correctness bug, so every lookup bypasses it.
FRESH = {"Cache-Control": "no-cache"}


class State:
    def __init__(self, client: FhirClient):
        self.c = client

    def _search(self, rtype: str, params: dict | None = None, *, limit: int | None = None) -> list[dict]:
        return self.c.search(rtype, params, limit=limit, headers=FRESH)

    # ---- people
    def practitioners(self) -> list[dict]:
        out = []
        for pr in self._search("PractitionerRole", {"_count": 100}):
            ref = pr.get("practitioner", {})
            role = pr.get("code", [{}])[0].get("coding", [{}])[0].get("code", "")
            out.append({"id": _ref_id(ref.get("reference")), "name": ref.get("display", ""), "role": role.capitalize()})
        return sorted(out, key=lambda p: (p["role"], p["name"]))

    def practitioner_role(self, pid: str) -> str | None:
        found = self._search("PractitionerRole", {"practitioner": f"Practitioner/{pid}"})
        return found[0]["code"][0]["coding"][0]["code"].capitalize() if found else None

    # ---- patients
    def patient(self, pid: str) -> dict | None:
        try:
            return self.c.read("Patient", pid).resource
        except FhirError as e:
            if e.status in (404, 410):
                return None
            raise

    def active_babies(self) -> list[dict]:
        out = []
        for e in self._search("Encounter", {"status": "in-progress", "_count": 100}):
            sub = e.get("subject", {})
            out.append({"id": _ref_id(sub.get("reference")), "name": sub.get("display", ""), "since": e["period"]["start"]})
        return sorted(out, key=lambda b: b["since"], reverse=True)

    def encounter(self, pid: str) -> dict | None:
        found = self._search("Encounter", {"subject": f"Patient/{pid}", "status": "in-progress"})
        return found[0] if found else None

    def mother(self, pid: str) -> dict | None:
        found = self._search("RelatedPerson", {"patient": f"Patient/{pid}", "relationship": "|".join(MOTHER_REL)})
        return found[0] if found else None

    def possible_duplicates(self, phone: str, dob: str) -> list[str]:
        """Patients whose mother has this phone and who share the date of birth (twin or duplicate)."""
        ids = []
        for rp in self._search("RelatedPerson", {"telecom": phone}):
            p = self.patient(_ref_id(rp["patient"]["reference"]))
            if p and p.get("birthDate") == dob:
                ids.append(p["id"])
        return ids

    # ---- clinical data
    def birth_weight(self, pid: str) -> float | None:
        r = self._search("Observation", {"subject": f"Patient/{pid}", "code": f"{LOINC}|{LOINC_BIRTH_WEIGHT}"}, limit=1)
        return r[0]["valueQuantity"]["value"] if r else None

    def last_weight(self, pid: str) -> tuple[float, str] | None:
        r = self._search(
            "Observation",
            {"subject": f"Patient/{pid}", "code": f"{LOINC}|{LOINC_WEIGHT}", "_sort": "-date", "_count": 1},
            limit=1,
        )
        return (r[0]["valueQuantity"]["value"], r[0].get("effectiveDateTime", "")) if r else None

    def consents(self, pid: str) -> list[dict]:
        return self._search("Consent", {"patient": f"Patient/{pid}"})

    def devices_in_situ(self, pid: str) -> list[dict]:
        return self._search("Procedure", {"subject": f"Patient/{pid}", "status": "in-progress"})
