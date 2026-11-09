"""Form values -> FHIR transaction Bundle.

Rules used throughout (each is a production habit worth noticing):
  * One operation = one transaction Bundle: all entries succeed or none do.
  * Every created resource carries an identifier `urn:h2f:idempotency|<key>/<role>` and is sent as a
    *conditional create* (ifNoneExist). Retrying the same submit returns 200 + the existing resources
    instead of creating duplicates. (Provenance has no identifier in R4, so it is tagged instead.)
  * Updates to existing resources are read-modify-write with `ifMatch` (optimistic locking).
  * A Provenance records who did it. In production that is a legal audit requirement.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fhirlab.core.fhir import LOINC, MRN_SYSTEM, OBS_CATEGORY, UCUM, urn
from fhirlab.clients.nicu.checks import ABHA_SYSTEM, Ctx, device_code
from fhirlab.clients.nicu.state import IDEM, MOTHER_REL, NICU_UHID

EXT = "http://hospital.example.org/StructureDefinition/"
PROC_SYSTEM = "urn:h2f:nicu-procedure"
V3_ACT = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
GENDER = {"Male": "male", "Female": "female", "Ambiguous": "other"}
DISPOSITION = {"Discharged home": "home", "Referred / transferred": "other-hcf", "LAMA": "aadvice", "Death": "exp"}
ANTHRO = {
    "weight": ("29463-7", "Body weight", "kg"),
    "length": ("8306-3", "Body length", "cm"),
    "ofc": ("9843-4", "Head Occipital-frontal circumference", "cm"),
}


def baby_id(idem: str) -> str:
    """Deterministic so a retry mints the same ID. A real system uses a DB sequence + check digit."""
    return "NICU-" + hashlib.sha256(idem.encode()).hexdigest()[:8].upper()


class Builder:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.entries: list[dict] = []

    def url(self, role: str) -> str:
        return urn(self.ctx.idem, role)

    def create(self, role: str, r: dict) -> str:
        """Conditional create. Returns the fullUrl so later entries can reference it."""
        full = self.url(role)
        key = f"{self.ctx.idem}/{role}"
        r.setdefault("identifier", []).append({"system": IDEM, "value": key})
        self.entries.append(
            {"fullUrl": full, "resource": r, "request": {"method": "POST", "url": r["resourceType"], "ifNoneExist": f"identifier={IDEM}|{key}"}}
        )
        return full

    def update(self, r: dict) -> str:
        """Read-modify-write: PUT the whole resource guarded by the version we read."""
        ref = f"{r['resourceType']}/{r['id']}"
        req = {"method": "PUT", "url": ref}
        if r.get("meta", {}).get("versionId"):
            req["ifMatch"] = f'W/"{r["meta"]["versionId"]}"'
        # a Bundle fullUrl must be absolute; the *returned* ref stays relative for use in references
        self.entries.append({"fullUrl": f"{self.ctx.state.c.base}/{ref}", "resource": r, "request": req})
        return ref

    def provenance(self, activity: str, reason: str | None = None) -> None:
        a = self.ctx.actor
        prov = {
            "resourceType": "Provenance",
            "meta": {"tag": [{"system": IDEM, "code": f"{self.ctx.idem}/prov"}]},
            "target": [
                {"reference": e["fullUrl"] if e["fullUrl"].startswith("urn:") else e["request"]["url"]}
                for e in self.entries
            ],
            "recorded": self.ctx.now.isoformat(),
            "activity": {"text": activity},
            "agent": [{
                "type": {"text": a["role"]},
                "who": {"reference": f"Practitioner/{a['sub']}", "display": a["name"]},
            }],
        }
        if reason:
            prov["reason"] = [{"text": reason}]
        self.entries.append({
            "fullUrl": self.url("prov"), "resource": prov,
            "request": {"method": "POST", "url": "Provenance", "ifNoneExist": f"_tag={IDEM}|{self.ctx.idem}/prov"},
        })

    def bundle(self) -> dict:
        return {"resourceType": "Bundle", "type": "transaction", "entry": self.entries}


def _qty(value, unit):
    return {"value": value, "unit": unit, "system": UCUM, "code": unit}


def _obs(code: str, display: str, subject: str, enc: str, when: str, value: dict, actor: dict, category="vital-signs") -> dict:
    return {
        "resourceType": "Observation",
        "status": "final",
        "category": [{"coding": [{"system": OBS_CATEGORY, "code": category}]}],
        "code": {"coding": [{"system": LOINC, "code": code, "display": display}], "text": display},
        "subject": {"reference": subject},
        "encounter": {"reference": enc},
        "effectiveDateTime": when,
        "performer": [{"reference": f"Practitioner/{actor['sub']}", "display": actor["name"]}],
        "valueQuantity": value,
    }


# --------------------------------------------------------------------------- operations
def map_operation(op_id: str, p: dict, ctx: Ctx) -> tuple[dict, dict]:
    """Returns (bundle, derived) where `derived` are values the middleware computed (shown in the UI)."""
    b = Builder(ctx)
    derived = _MAPPERS[op_id](b, p, ctx)
    return b.bundle(), derived


def _admit(b: Builder, p: dict, ctx: Ctx) -> dict:
    when = ctx.event_time.isoformat()
    bid = baby_id(ctx.idem)
    ga_days = p["ga_weeks"] * 7 + p.get("ga_days", 0)
    patient: dict[str, Any] = {
        "resourceType": "Patient",
        "identifier": [{"system": NICU_UHID, "value": bid, "use": "official"}],
        "active": True,
        "name": [{"use": "official", "text": p["baby_name"]}],
        "gender": GENDER[p["sex"]],
        "birthDate": p["dob"],
        "extension": [],
    }
    if p["sex"] == "Ambiguous":
        patient["extension"].append({"url": EXT + "sex-recorded-as", "valueString": "Ambiguous"})  # FHIR gender has no 'ambiguous'
    if "tob" in p:
        # birthTime extends the *primitive* birthDate, so it lives under `_birthDate`, not under Patient.extension
        patient["_birthDate"] = {"extension": [{"url": "http://hl7.org/fhir/StructureDefinition/patient-birthTime", "valueDateTime": f"{p['dob']}T{p['tob']}:00{when[-6:]}"}]}
    if "place_of_birth" in p:
        patient["extension"].append({"url": "http://hl7.org/fhir/StructureDefinition/patient-birthPlace", "valueAddress": {"text": p["place_of_birth"]}})
    if not patient["extension"]:
        del patient["extension"]
    pat = b.create("patient", patient)

    mother: dict[str, Any] = {
        "resourceType": "RelatedPerson",
        "active": True,
        "patient": {"reference": pat, "display": p["baby_name"]},
        "relationship": [{"coding": [{"system": MOTHER_REL[0], "code": MOTHER_REL[1], "display": "mother"}]}],
        "name": [{"text": p["mother_name"]}],
        "telecom": [{"system": "phone", "value": p["phone"][-10:], "use": "mobile"}],
        "identifier": [],
        "extension": [],
    }
    if "address" in p:
        mother["address"] = [{"text": p["address"]}]
    if "mother_uhid" in p:
        mother["identifier"].append({"system": MRN_SYSTEM, "value": p["mother_uhid"]})
    if "mother_age" in p:
        mother["extension"].append({"url": EXT + "mother-age-years", "valueInteger": p["mother_age"]})
    if p.get("blood_group", "-") != "-":
        mother["extension"].append({"url": EXT + "mother-blood-group", "valueString": p["blood_group"]})
    mother["extension"] = mother["extension"] or None
    mother = {k: v for k, v in mother.items() if v is not None}
    b.create("mother", mother)

    attending = ctx.scratch.get("attending") or {}
    participant = {"type": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ParticipationType", "code": "ATND"}]}]}
    if p["attending"] == "Other":
        participant["individual"] = {"display": p["doctor_name"]}
    else:
        participant["individual"] = {"reference": f"Practitioner/{p['attending']}", "display": attending.get("name", "")}
    enc = b.create("encounter", {
        "resourceType": "Encounter",
        "status": "in-progress",
        "class": {"system": V3_ACT, "code": "IMP", "display": "inpatient encounter"},
        "serviceType": {"text": "Neonatal intensive care"},
        "subject": {"reference": pat, "display": p["baby_name"]},
        "participant": [participant],
        "period": {"start": when},
        "reasonCode": [{"text": p["diagnosis"]}],
    })
    b.create("dx", {
        "resourceType": "Condition",
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "encounter-diagnosis"}]}],
        "code": {"text": p["diagnosis"]},
        "subject": {"reference": pat},
        "encounter": {"reference": enc},
        "recordedDate": when,
    })
    birth_when = f"{p['dob']}T{p['tob']}:00{when[-6:]}" if "tob" in p else p["dob"]
    b.create("bw", _obs("8339-4", "Birth weight Measured", pat, enc, birth_when, _qty(p["birth_weight"], "kg"), ctx.actor, "exam"))
    b.create("ga", _obs("18185-9", "Gestational age", pat, enc, birth_when, _qty(ga_days, "d"), ctx.actor, "exam"))
    reason = None
    if p["abha_choice"] == "Skip for now":
        reason = "ABHA skipped" + (f": {p['abha_skip_reason']}" if "abha_skip_reason" in p else "")
    b.provenance("patient.admit", reason)
    return {"baby_id": bid, "gestational_age_days": ga_days, "gestational_age": f"{p['ga_weeks']}+{p.get('ga_days', 0)} wk", "fhir_gender": GENDER[p["sex"]]}


def _ref(ctx: Ctx) -> tuple[str, str]:
    return f"Patient/{ctx.patient_id}", f"Encounter/{ctx.scratch['encounter']['id']}"


def _anthro(b: Builder, p: dict, ctx: Ctx) -> dict:
    pat, enc = _ref(ctx)
    when = ctx.event_time.isoformat()
    made = []
    for key, (code, display, unit) in ANTHRO.items():
        if key in p:
            b.create(key, _obs(code, display, pat, enc, when, _qty(p[key], unit), ctx.actor))
            made.append(f"{display} {p[key]} {unit}")
    b.provenance("anthro.record")
    return {"observations": made}


SCOPE_CODE = {"system": "http://terminology.hl7.org/CodeSystem/consentscope", "code": "patient-privacy"}


def _consent(b: Builder, p: dict, ctx: Ctx) -> dict:
    pat, _ = _ref(ctx)
    when = ctx.event_time.isoformat()
    ext = [{"url": EXT + "consent-given-by", "valueString": p["given_by"]}] if "given_by" in p else []
    if p.get("note"):
        ext.append({"url": EXT + "consent-note", "valueString": p["note"]})
    active = ctx.scratch.get("consent_active")
    if p["action"] == "Record":
        c = {
            "resourceType": "Consent", "status": "active", "scope": {"coding": [SCOPE_CODE]},
            "category": [{"text": p["scope"]}], "patient": {"reference": pat}, "dateTime": when,
            "policyRule": {"coding": [{"system": V3_ACT, "code": "OPTIN", "display": "opt-in"}]},  # R4 rule ppc-1
        }
        if ext:
            c["extension"] = ext
        b.create("consent", c)
    else:
        c = {**active, "dateTime": when}
        if p["action"] == "Withdraw":
            c["status"] = "inactive"
        elif ext:
            c["extension"] = ext
        b.update(c)
    b.provenance(f"consent.{p['action'].lower()}")
    return {"consent_status": "inactive" if p["action"] == "Withdraw" else "active", "versioned_update": p["action"] != "Record"}


def _abha(b: Builder, p: dict, ctx: Ctx) -> dict:
    mother = dict(ctx.scratch["mother"])
    ids = [i for i in mother.get("identifier", []) if i.get("system") != ABHA_SYSTEM]
    ids.append({"system": ABHA_SYSTEM, "value": p["abha_number"], "use": "official", "type": {"text": "ABHA number"}})
    mother["identifier"] = ids
    ext = [e for e in mother.get("extension", []) if e["url"] != EXT + "abha-verified"]
    ext.append({"url": EXT + "abha-verified", "valueBoolean": p["verified"] == "Yes"})
    if "abha_address" in p:
        ext.append({"url": EXT + "abha-address", "valueString": p["abha_address"]})
    mother["extension"] = ext
    b.update(mother)
    b.provenance("abha.link")
    return {"updates": f"RelatedPerson/{mother['id']} (If-Match version {mother['meta']['versionId']})"}


def _procedure(b: Builder, p: dict, ctx: Ctx) -> dict:
    pat, enc = _ref(ctx)
    when = ctx.event_time.isoformat()
    code = device_code(p)
    label = p.get("nb_procedure") or p["ptype"] + (f" ({p['cl_device']})" if "cl_device" in p else "")
    proc: dict[str, Any] = {
        "resourceType": "Procedure",
        "subject": {"reference": pat},
        "encounter": {"reference": enc},
        "performer": [{"actor": {"reference": f"Practitioner/{ctx.actor['sub']}", "display": ctx.actor["name"]}}],
    }
    notes = []
    if p["action"] == "Insertion":
        bundle_item = None
        proc |= {
            "status": "completed" if p["ptype"] == "Non-bundle procedure" else "in-progress",  # in-progress == device in situ
            "code": {"coding": [{"system": PROC_SYSTEM, "code": code, "display": label}], "text": label},
            "performedPeriod": {"start": when},
        }
        if "cl_site" in p:
            proc["bodySite"] = [{"text": p["cl_site"]}]
        for k in ("nb_indication", "uc_indication"):
            if k in p:
                proc["reasonCode"] = [{"text": p[k]}]
        if "nb_outcome" in p:
            proc["outcome"] = {"text": p["nb_outcome"]}
        if "nb_operator" in p:
            notes.append(f"Operator: {p['nb_operator']}")
        if notes:
            proc["note"] = [{"text": n} for n in notes]
        purl = b.create("proc", proc)
        items = {k: v for k, v in p.items() if k.split("_")[0] in ("cl", "ett", "uc") and k != "cl_device"}
        if items and p["ptype"] != "Non-bundle procedure":
            compliant = not any(v in {"Not Done", "No", "No - breach", "Not done", "Partial"} for v in items.values())
            b.create("bundle", {
                "resourceType": "Observation", "status": "final",
                "category": [{"coding": [{"system": OBS_CATEGORY, "code": "procedure"}]}],
                "code": {"coding": [{"system": "urn:h2f:nicu-bundle", "code": f"{code.split('-')[0]}-insertion-bundle"}], "text": f"{label} insertion bundle"},
                "subject": {"reference": pat}, "encounter": {"reference": enc}, "partOf": [{"reference": purl}],
                "effectiveDateTime": when,
                "valueString": "compliant" if compliant else "non-compliant",
                "component": [{"code": {"text": k}, "valueString": str(v)} for k, v in items.items()],
            })
            bundle_item = "compliant" if compliant else "non-compliant"
        b.provenance(f"procedure.insertion:{code}")
        return {"device_code": code, "device_state": "in situ" if proc["status"] == "in-progress" else "one-off", "bundle": bundle_item}

    # removal: close the insertion (PUT, version-guarded) and log the removal as its own Procedure
    ins = dict(ctx.scratch["in_situ"][0])
    ins["status"] = "completed"
    ins["performedPeriod"] = {**ins.get("performedPeriod", {}), "end": when}
    iref = b.update(ins)
    reasons = [p["rm_reason"]]
    proc |= {
        "status": "completed",
        "code": {"coding": [{"system": PROC_SYSTEM, "code": f"{ins['code']['coding'][0]['code']}-removal", "display": f"{label} removal"}], "text": f"{label} removal"},
        "partOf": [{"reference": iref}],
        "performedDateTime": when,
        "reasonCode": [{"text": r} for r in reasons],
        "outcome": {"text": p["rm_complication"]},
    }
    if p.get("rm_note"):
        proc["note"] = [{"text": p["rm_note"]}]
    if "rm_culture" in p:
        proc.setdefault("note", []).append({"text": f"Tip/device sent for culture: {p['rm_culture']}"})
    b.create("removal", proc)
    b.provenance(f"procedure.removal:{ins['code']['coding'][0]['code']}")
    return {"device_code": ins["code"]["coding"][0]["code"], "device_state": "removed", "closes": iref}


def _discharge(b: Builder, p: dict, ctx: Ctx) -> dict:
    enc = dict(ctx.scratch["encounter"])
    when = ctx.event_time.isoformat()
    enc["status"] = "finished"
    enc["period"] = {**enc["period"], "end": when}
    enc["hospitalization"] = {"dischargeDisposition": {
        "coding": [{"system": "http://terminology.hl7.org/CodeSystem/discharge-disposition", "code": DISPOSITION[p["outcome"]]}],
        "text": p["outcome"],
    }}
    ext = [e for e in enc.get("extension", []) if not e["url"].startswith(EXT + "discharge-")]
    ext.append({"url": EXT + "discharge-summary", "valueString": p["summary"]})
    for k, u in (("follow_up", "follow-up"), ("destination", "destination")):
        if p.get(k):
            ext.append({"url": EXT + f"discharge-{u}", "valueString": p[k]})
    enc["extension"] = ext
    eref = b.update(enc)
    if "discharge_weight" in p:
        b.create("dw", _obs("29463-7", "Body weight", f"Patient/{ctx.patient_id}", eref, when, _qty(p["discharge_weight"], "kg"), ctx.actor))
    if p["outcome"] == "Death":
        pat = ctx.state.patient(ctx.patient_id)
        pat["deceasedDateTime"] = when
        b.update(pat)
    b.provenance(f"patient.discharge:{p['outcome']}")
    return {"closes": eref, "disposition": DISPOSITION[p["outcome"]]}


_MAPPERS = {
    "patient.admit": _admit,
    "anthro.record": _anthro,
    "consent.record": _consent,
    "abha.link": _abha,
    "procedure.log": _procedure,
    "patient.discharge": _discharge,
}

__all__ = ["map_operation", "baby_id"]
