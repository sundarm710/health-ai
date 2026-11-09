"""Seed the reference data the simulation needs: one hospital and four practitioners.

`uv run python -m h2f.sim.seed` (or `make sim-seed`). Safe to run repeatedly: every entry is a
conditional create keyed on an identifier, so a second run changes nothing. Names are synthetic.
"""

from __future__ import annotations

from h2f.client import FhirClient
from h2f.fhir import urn
from h2f.sim.state import ROLE_SYSTEM

PRACTITIONER_SYSTEM = "http://hospital.example.org/practitioner-id"
ORG_SYSTEM = "http://hospital.example.org/organization-id"
STAFF = [
    ("DR-001", "Meera", "Iyer", "doctor"),
    ("DR-002", "Arjun", "Menon", "doctor"),
    ("RN-001", "Kavya", "Nair", "nurse"),
    ("RN-002", "Rohan", "Das", "nurse"),
]


def seed_bundle() -> dict:
    org = urn("seed", "org")
    entries = [{
        "fullUrl": org,
        "resource": {"resourceType": "Organization", "identifier": [{"system": ORG_SYSTEM, "value": "NICU-HOSP-1"}],
                     "name": "Demo Neonatal Hospital", "active": True},
        "request": {"method": "POST", "url": "Organization", "ifNoneExist": f"identifier={ORG_SYSTEM}|NICU-HOSP-1"},
    }]
    for code, given, family, role in STAFF:
        prac = urn("seed", code)
        title = "Dr." if role == "doctor" else "Nurse"
        entries.append({
            "fullUrl": prac,
            "resource": {"resourceType": "Practitioner", "identifier": [{"system": PRACTITIONER_SYSTEM, "value": code}],
                         "name": [{"family": family, "given": [given], "prefix": [title], "text": f"{title} {given} {family}"}], "active": True},
            "request": {"method": "POST", "url": "Practitioner", "ifNoneExist": f"identifier={PRACTITIONER_SYSTEM}|{code}"},
        })
        entries.append({
            "fullUrl": urn("seed", f"{code}-role"),
            "resource": {"resourceType": "PractitionerRole", "identifier": [{"system": PRACTITIONER_SYSTEM, "value": f"{code}-role"}],
                         "practitioner": {"reference": prac, "display": f"{title} {given} {family}"},
                         "organization": {"reference": org},
                         "code": [{"coding": [{"system": ROLE_SYSTEM, "code": role}], "text": role.capitalize()}], "active": True},
            "request": {"method": "POST", "url": "PractitionerRole", "ifNoneExist": f"identifier={PRACTITIONER_SYSTEM}|{code}-role"},
        })
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def main() -> None:
    c = FhirClient(trace=True)
    r = c.transaction(seed_bundle())
    created = sum(e["response"]["status"].startswith("201") for e in r.resource["entry"])
    print(f"seed: {created} created, {len(r.resource['entry']) - created} already existed")


if __name__ == "__main__":
    main()
