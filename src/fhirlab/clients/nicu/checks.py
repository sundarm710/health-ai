"""Clinical (cross-field) and state (server-side) consistency checks.

Four kinds of inconsistency, deliberately kept apart so each message says what kind of problem it is:
  field    -> catalog.parse_fields (type, required, range, options)
  clinical -> this module: the values contradict each other or common sense
  state    -> this module: the values contradict what the server already holds
  authz    -> pipeline: this actor may not do this
These are simulation heuristics, not clinical guidance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from fhirlab.clients.nicu.catalog import Issue
from fhirlab.clients.nicu.state import State

BUNDLE_BAD = {"Not Done", "No", "No - breach", "Not done", "Partial"}


@dataclass
class Ctx:
    now: datetime
    event_time: datetime
    actor: dict
    state: State
    patient_id: str | None = None
    idem: str = ""
    scratch: dict = field(default_factory=dict)  # lookups done during checks, reused by the mapper


def device_code(p: dict) -> str:
    t = p.get("ptype")
    if t == "Central line":
        return f"central-line-{p.get('cl_device', 'unspecified')}"
    return {"Endotracheal intubation": "ett", "Urinary catheter": "urinary-catheter"}.get(t, f"nb-{p.get('nb_procedure', 'other')}")


def _err(stage: str, path: str, msg: str) -> Issue:
    return Issue("error", stage, path, msg)


def _warn(stage: str, path: str, msg: str) -> Issue:
    return Issue("warning", stage, path, msg)


def _birth_dt(p: dict, tz) -> datetime:
    return datetime.combine(datetime.fromisoformat(p["dob"]).date(), time.fromisoformat(p.get("tob", "00:00")), tzinfo=tz)


# --------------------------------------------------------------------------- clinical
def clinical(op_id: str, p: dict, ctx: Ctx) -> list[Issue]:
    out: list[Issue] = []
    if ctx.event_time > ctx.now + timedelta(minutes=5):
        out.append(_err("clinical", "event_time", "Event time is in the future"))
    fn = _CLINICAL.get(op_id)
    return out + (fn(p, ctx) if fn else [])


def _admit_clinical(p: dict, ctx: Ctx) -> list[Issue]:
    out = []
    born = _birth_dt(p, ctx.event_time.tzinfo)
    if born > ctx.event_time:
        out.append(_err("clinical", "dob", "Birth is after the admission time; check date/time of birth or the admission time"))
    elif ctx.event_time - born > timedelta(days=28):
        out.append(_warn("clinical", "dob", "Baby is over 28 days old at admission; unusual for a neonatal unit"))
    ga, w = p["ga_weeks"] + p.get("ga_days", 0) / 7, p["birth_weight"]
    if p["ga_weeks"] == 44 and p.get("ga_days", 0) > 0:
        out.append(_err("clinical", "ga_days", "Gestational age cannot exceed 44+0 weeks"))
    if ga < 28 and w > 2.0:
        out.append(_warn("clinical", "birth_weight", f"{w} kg is heavy for {p['ga_weeks']} weeks; check weight or gestational age"))
    if ga >= 37 and w < 2.0:
        out.append(_warn("clinical", "birth_weight", f"{w} kg is low for a term baby ({p['ga_weeks']} weeks); check weight or gestational age"))
    if "mother_age" in p and not 15 <= p["mother_age"] <= 45:
        out.append(_warn("clinical", "mother_age", f"Mother age {p['mother_age']} is unusual; please confirm"))
    return out


def _anthro_clinical(p: dict, ctx: Ctx) -> list[Issue]:
    if not any(k in p for k in ("weight", "length", "ofc")):
        return [_err("clinical", "weight", "Enter at least one of weight, length or head circumference")]
    return []


def _procedure_clinical(p: dict, ctx: Ctx) -> list[Issue]:
    out = []
    t, action = p["ptype"], p["action"]
    if t == "Non-bundle procedure" and action == "Removal":
        out.append(_err("clinical", "action", "Non-bundle procedures are one-off; there is nothing to remove"))
    if action == "Insertion":
        if t == "Central line":
            bad = [k for k in ("cl_hand_hygiene", "cl_barrier", "cl_dry", "cl_sterile_field", "cl_drape") if p.get(k) in BUNDLE_BAD]
            if bad:
                out.append(_warn("clinical", bad[0], "CLABSI bundle not fully met (" + ", ".join(bad) + "). Consider filing an incident (Form 11)."))
            if p.get("cl_attempts", 1) > 3:
                out.append(_warn("clinical", "cl_attempts", f"{p['cl_attempts']} attempts; review technique / escalate"))
        if t == "Endotracheal intubation":
            if p.get("ett_hand_hygiene") in BUNDLE_BAD or p.get("ett_gloves") == "No":
                out.append(_warn("clinical", "ett_hand_hygiene", "VAP bundle item missed on intubation"))
        if t == "Urinary catheter" and p.get("uc_asepsis") in BUNDLE_BAD:
            out.append(_warn("clinical", "uc_asepsis", "CAUTI bundle: aseptic technique not fully done"))
    if t == "Non-bundle procedure" and p.get("nb_outcome") == "Complication - incident filed":
        out.append(_warn("clinical", "nb_outcome", "Complication reported; make sure the incident report (Form 11) exists"))
    if action == "Removal" and p.get("rm_complication") == "Infection":
        out.append(_warn("clinical", "rm_complication", "Infection on removal: consider reporting a suspected HAI (Form 27)"))
    return out


def _discharge_clinical(p: dict, ctx: Ctx) -> list[Issue]:
    if p["outcome"] == "Death" and len(p["summary"]) < 20:
        return [_err("clinical", "summary", "A death needs a fuller summary (at least 20 characters)")]
    return []


_CLINICAL: dict[str, Any] = {
    "patient.admit": _admit_clinical,
    "anthro.record": _anthro_clinical,
    "procedure.log": _procedure_clinical,
    "patient.discharge": _discharge_clinical,
}


# --------------------------------------------------------------------------- state
def server_state(op_id: str, p: dict, ctx: Ctx) -> list[Issue]:
    """Checks that need to read HAPI. Returns issues; also stashes lookups on ctx for the mapper."""
    st = ctx.state
    out: list[Issue] = []
    if op_id == "patient.admit":
        dup = st.possible_duplicates(p["phone"][-10:], p["dob"])
        if dup:
            out.append(_warn("state", "phone", f"Patient(s) {', '.join(dup)} share this mother's phone and birth date: twin, or a duplicate registration?"))
        return out

    # everything else needs an existing patient with an active admission
    if not ctx.patient_id:
        return [_err("state", "patient", "Select a patient")]
    if not st.patient(ctx.patient_id):
        return [_err("state", "patient", f"Patient/{ctx.patient_id} does not exist")]
    enc = st.encounter(ctx.patient_id)
    ctx.scratch["encounter"] = enc
    if not enc:
        return [_err("state", "patient", "This baby has no active admission (already discharged?)")]
    if ctx.event_time < datetime.fromisoformat(enc["period"]["start"]):
        out.append(_err("state", "event_time", f"Event time is before the admission ({enc['period']['start']})"))

    fn = _STATE.get(op_id)
    return out + (fn(p, ctx) if fn else [])


def _anthro_state(p: dict, ctx: Ctx) -> list[Issue]:
    out, st = [], ctx.state
    if "weight" in p:
        bw = st.birth_weight(ctx.patient_id)
        if bw and p["weight"] < 0.75 * bw:
            out.append(_warn("state", "weight", f"{p['weight']} kg is more than 25% below birth weight ({bw} kg)"))
        if bw and p["weight"] > 3 * bw:
            out.append(_err("state", "weight", f"{p['weight']} kg is over 3x birth weight ({bw} kg); likely a typo"))
        last = st.last_weight(ctx.patient_id)
        if last and abs(p["weight"] - last[0]) / last[0] > 0.10:
            out.append(_warn("state", "weight", f"Changed {100 * (p['weight'] - last[0]) / last[0]:+.0f}% from the last weight ({last[0]} kg)"))
    return out


def _consent_state(p: dict, ctx: Ctx) -> list[Issue]:
    existing = [c for c in ctx.state.consents(ctx.patient_id) if c.get("category", [{}])[0].get("text") == p["scope"]]
    active = [c for c in existing if c["status"] == "active"]
    ctx.scratch["consent_active"] = active[0] if active else None
    if p["action"] == "Record" and active:
        return [_warn("state", "action", "An active consent for this scope already exists; use Update to revise it")]
    if p["action"] in ("Update", "Withdraw") and not active:
        return [_err("state", "action", f"No active '{p['scope']}' consent to {p['action'].lower()}")]
    return []


def _abha_state(p: dict, ctx: Ctx) -> list[Issue]:
    mother = ctx.state.mother(ctx.patient_id)
    ctx.scratch["mother"] = mother
    if not mother:
        return [_err("state", "patient", "No mother (RelatedPerson) on file for this baby")]
    have = [i["value"] for i in mother.get("identifier", []) if i.get("system") == ABHA_SYSTEM]
    if have and have[0] != p["abha_number"]:
        return [_err("state", "abha_number", f"Mother already has ABHA {have[0]}; it cannot be silently replaced")]
    if have:
        return [_warn("state", "abha_number", "This ABHA is already linked; submitting changes nothing")]
    return []


def _procedure_state(p: dict, ctx: Ctx) -> list[Issue]:
    situ = ctx.state.devices_in_situ(ctx.patient_id)
    code = device_code(p)
    match = [x for x in situ if x.get("code", {}).get("coding", [{}])[0].get("code") == code]
    ctx.scratch["in_situ"] = match
    if p["action"] == "Removal":
        if p["ptype"] == "Central line" and "cl_device" not in p:
            lines = [x for x in situ if x["code"]["coding"][0]["code"].startswith("central-line-")]
            if len(lines) == 1:
                ctx.scratch["in_situ"] = lines
            elif not lines:
                return [_err("state", "ptype", "No central line is in situ to remove")]
            else:
                return [_err("state", "cl_device", "Several lines are in situ; choose which one")]
        elif not match:
            return [_err("state", "ptype", f"No '{code}' is in situ to remove")]
    elif match and p["ptype"] != "Non-bundle procedure":
        return [_warn("state", "ptype", f"A '{code}' is already in situ; this logs a second one")]
    return []


def _discharge_state(p: dict, ctx: Ctx) -> list[Issue]:
    out, st = [], ctx.state
    situ = st.devices_in_situ(ctx.patient_id)
    if situ:
        names = ", ".join(x["code"]["coding"][0]["code"] for x in situ)
        out.append(_warn("state", "outcome", f"Devices still in situ: {names}. Log removals before discharge?"))
    last = st.last_weight(ctx.patient_id)
    if "discharge_weight" in p and last and abs(p["discharge_weight"] - last[0]) / last[0] > 0.15:
        out.append(_warn("state", "discharge_weight", f"Differs >15% from the last recorded weight ({last[0]} kg)"))
    return out


ABHA_SYSTEM = "https://healthid.abdm.gov.in"

_STATE: dict[str, Any] = {
    "anthro.record": _anthro_state,
    "consent.record": _consent_state,
    "abha.link": _abha_state,
    "procedure.log": _procedure_state,
    "patient.discharge": _discharge_state,
}
