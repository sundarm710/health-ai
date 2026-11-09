"""FHIR R4 conventions shared by the golden builder, rules mapper, agents and eval."""

from __future__ import annotations

import uuid

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
UCUM = "http://unitsofmeasure.org"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
LOCAL_LAB = "urn:h2f:local-lab"  # what the rules mapper emits for site-local (non-LOINC) codes

MRN_SYSTEM = "http://hospital.example.org/mrn"
VISIT_SYSTEM = "http://hospital.example.org/visit"
FILLER_SYSTEM = "http://hospital.example.org/lab-filler"

ASSIGNING_AUTHORITY_TO_SYSTEM = {"HOSP": MRN_SYSTEM}
V2_CODING_SYSTEM = {"LN": LOINC, "SCT": SNOMED, "UCUM": UCUM}
ALLOWED_TYPES = {"Patient", "Encounter", "Observation", "DiagnosticReport"}

_NS = uuid.UUID("6f1c9a2e-0000-4000-8000-000000000000")


def urn(msg_id: str, role: str) -> str:
    """Deterministic urn:uuid so reruns produce identical bundles."""
    return f"urn:uuid:{uuid.uuid5(_NS, f'{msg_id}/{role}')}"


def identifier_query(resource: dict) -> str | None:
    ids = resource.get("identifier") or []
    if ids and ids[0].get("value"):
        sys = ids[0].get("system", "")
        return f"identifier={sys}|{ids[0]['value']}" if sys else f"identifier={ids[0]['value']}"
    return None


def transaction(entries: list[tuple[str, dict]], msg_type: str) -> dict:
    """entries: [(fullUrl, resource)]. Patient on A08 is a conditional update (PUT),
    everything identifiable is a conditional create, Observations are plain creates."""
    out = []
    for full_url, r in entries:
        rt = r["resourceType"]
        q = identifier_query(r)
        if rt == "Patient" and msg_type == "ADT^A08" and q:
            req = {"method": "PUT", "url": f"Patient?{q}"}
        elif q:
            req = {"method": "POST", "url": rt, "ifNoneExist": q}
        else:
            req = {"method": "POST", "url": rt}
        out.append({"fullUrl": full_url, "resource": r, "request": req})
    return {"resourceType": "Bundle", "type": "transaction", "entry": out}
