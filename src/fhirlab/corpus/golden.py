"""MessageFacts -> golden FHIR transaction Bundle (the projection of Synthea onto v2)."""

from __future__ import annotations

from fhirlab import fhir as F
from fhirlab.corpus.facts import MessageFacts


def golden_bundle(f: MessageFacts) -> dict:
    p, e = f.patient, f.encounter
    pat_url, enc_url = F.urn(f.msg_id, "patient"), F.urn(f.msg_id, "encounter")
    patient = {
        "resourceType": "Patient",
        "identifier": [{"system": F.MRN_SYSTEM, "value": p.mrn}],
        "name": [{"family": p.family, "given": p.given}],
        "gender": p.gender,
        "birthDate": p.birth_date,
        "address": [{"line": [p.line], "city": p.city, "state": p.state, "postalCode": p.postal, "country": "US"}],
        "telecom": [{"system": "phone", "value": p.phone, "use": "home"}],
    }
    encounter = {
        "resourceType": "Encounter",
        "identifier": [{"system": F.VISIT_SYSTEM, "value": e.visit_number}],
        "status": "finished" if e.end else "in-progress",
        "class": {"system": F.ACT_CODE, "code": e.cls},
        "subject": {"reference": pat_url},
        "period": {k: v for k, v in (("start", e.start), ("end", e.end)) if v},
    }
    entries = [(pat_url, patient), (enc_url, encounter)]

    if f.report:
        r = f.report
        obs_urls = []
        for i, o in enumerate(r.observations, 1):
            url = F.urn(f.msg_id, f"obs{i}")
            obs = {
                "resourceType": "Observation",
                "status": "final",
                "category": [{"coding": [{"system": F.OBS_CATEGORY, "code": "laboratory"}]}],
                "code": {"coding": [{"system": F.LOINC, "code": o.loinc, "display": o.display}]},
                "subject": {"reference": pat_url},
                "encounter": {"reference": enc_url},
                "effectiveDateTime": o.effective,
            }
            if o.value_type == "NM":
                obs["valueQuantity"] = {"value": o.value, "unit": o.unit, "system": F.UCUM, "code": o.unit}
            else:
                obs["valueCodeableConcept"] = {
                    "coding": [{"system": F.SNOMED, "code": o.value_code, "display": o.value_display}]
                }
            if o.note:
                obs["note"] = [{"text": o.note}]
            obs_urls.append(url)
            entries.append((url, obs))
        report = {
            "resourceType": "DiagnosticReport",
            "identifier": [{"system": F.FILLER_SYSTEM, "value": r.filler_id}],
            "status": "final",
            "category": [{"coding": [{"system": F.V2_0074, "code": "LAB"}]}],
            "code": {"coding": [{"system": F.LOINC, "code": r.loinc, "display": r.display}]},
            "subject": {"reference": pat_url},
            "encounter": {"reference": enc_url},
            "effectiveDateTime": r.effective,
            "issued": r.issued,
            "result": [{"reference": u} for u in obs_urls],
        }
        entries.insert(2, (F.urn(f.msg_id, "report"), report))
    return F.transaction(entries, f.msg_type)
