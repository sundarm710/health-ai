"""YAML-template rules mapper: v2 -> FHIR resources + typed field errors.

Contract: every field is either mapped exactly per the v2 spec, or recorded as a FieldError
with a failure kind. When a value can't be mapped correctly the mapper still emits the most
honest *valid* representation (local coding, CodeableConcept.text, unit without UCUM code) so
the bundle validates, and flags it. Validity is not correctness; the flags drive the agents.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path

import yaml

from h2f import fhir as F
from h2f.parse.parser import Parsed, Segment, parse
from h2f.v2 import v2_ts_to_fhir

TEMPLATES = Path(__file__).parent / "templates"


class FailureKind(StrEnum):
    Z_SEGMENT = "Z_SEGMENT"
    LOCAL_CODE = "LOCAL_CODE"
    FREETEXT_CODED = "FREETEXT_CODED"
    MISSING_REQUIRED = "MISSING_REQUIRED"
    UNIT = "UNIT"
    DATE = "DATE"
    ENCODING = "ENCODING"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    UNKNOWN = "UNKNOWN"


class TransformError(Exception):
    def __init__(self, kind: FailureKind, detail: str, fallback=None):
        super().__init__(detail)
        self.kind, self.detail, self.fallback = kind, detail, fallback


@dataclass
class FieldError:
    kind: str
    role: str  # which output resource, e.g. "obs3"
    source: str  # e.g. "OBX-6"
    seg_index: int
    value: str
    target: str  # FHIR path
    detail: str


@dataclass
class MapResult:
    msg_id: str
    msg_type: str
    resources: dict[str, dict]  # role -> resource (roles: patient, encounter, report, obs1..)
    errors: list[FieldError] = field(default_factory=list)
    unmapped: list[dict] = field(default_factory=list)  # whole segments nobody consumed
    consumed: set[tuple[int, int]] = field(default_factory=set)
    total_fields: int = 0

    @property
    def coverage(self) -> float:
        return len(self.consumed) / self.total_fields if self.total_fields else 1.0

    @property
    def clean(self) -> bool:
        return not self.errors and not self.unmapped

    def bundle(self) -> dict:
        return assemble(self.msg_id, self.msg_type, self.resources)

    def errors_dicts(self) -> list[dict]:
        return [asdict(e) for e in self.errors]


# --- transforms --------------------------------------------------------------------------------

_MOJIBAKE = re.compile(r"[ÃÂ][\x80-\xbf]")


@cache
def _ucum() -> frozenset[str]:
    return frozenset(yaml.safe_load((TEMPLATES / "units.yaml").read_text())["ucum"])


def t_v2_date(v: str):
    try:
        return v2_ts_to_fhir(v)[:10]
    except ValueError as e:
        raise TransformError(FailureKind.DATE, str(e)) from None


def t_v2_ts(v: str):
    try:
        return v2_ts_to_fhir(v)
    except ValueError as e:
        raise TransformError(FailureKind.DATE, str(e)) from None


def t_assigning_authority(v: str):
    if v in F.ASSIGNING_AUTHORITY_TO_SYSTEM:
        return F.ASSIGNING_AUTHORITY_TO_SYSTEM[v]
    raise TransformError(FailureKind.MISSING_REQUIRED, f"unknown/missing assigning authority {v!r}")


def t_phone(v: str):
    return {"system": "phone", "value": v, "use": "home"}


def t_patient_class(v: str):
    table = {"I": "IMP", "E": "EMER", "O": "AMB"}
    if v in table:
        return {"system": F.ACT_CODE, "code": table[v]}
    raise TransformError(FailureKind.FREETEXT_CODED, f"PV1-2 {v!r} not in table 0004")


def t_encounter_status(v: str):
    return "finished" if v else "in-progress"


def _looks_like_code(s: str) -> bool:
    return bool(s) and " " not in s and any(ch.isdigit() for ch in s)


def t_ce(comps: list[str]):
    code, display, system = (comps + ["", "", ""])[:3]
    if system in F.V2_CODING_SYSTEM:
        coding = {"system": F.V2_CODING_SYSTEM[system], "code": code}
        if display:
            coding["display"] = display
        return {"coding": [coding]}
    if system in ("L", "99LOCAL") or (system == "" and code.startswith("LAB-")):
        fb = {"coding": [{"system": F.LOCAL_LAB, "code": code, "display": display}]}
        raise TransformError(FailureKind.LOCAL_CODE, f"local code {code!r}", fb)
    if not system and code and display:
        fb = {"coding": [{"code": code, "display": display}]}
        raise TransformError(FailureKind.MISSING_REQUIRED, "coding system component missing", fb)
    if not system and code and not _looks_like_code(code):
        raise TransformError(FailureKind.FREETEXT_CODED, f"free text in coded field: {code!r}", {"text": code})
    raise TransformError(FailureKind.UNKNOWN, f"unrecognised coded value {comps!r}", {"text": " ".join(comps)})


def t_obx_value(parts: list):
    vtype, value_comps, unit_comps = parts
    raw = value_comps[0] if value_comps else ""
    if vtype == "NM":
        try:
            num = float(raw)
        except ValueError:
            raise TransformError(
                FailureKind.TYPE_MISMATCH, f"OBX-2=NM but OBX-5={raw!r}", ("valueString", raw)
            ) from None
        unit = unit_comps[0] if unit_comps else ""
        q = {"value": num}
        if unit in _ucum():
            return ("valueQuantity", {**q, "unit": unit, "system": F.UCUM, "code": unit})
        raise TransformError(FailureKind.UNIT, f"unit {unit!r} is not a known UCUM code", ("valueQuantity", {**q, "unit": unit}))
    if vtype == "CE":
        try:
            return ("valueCodeableConcept", t_ce(value_comps))
        except TransformError as e:
            raise TransformError(e.kind, e.detail, ("valueCodeableConcept", e.fallback)) from None
    if vtype in ("ST", "TX", "FT"):
        return ("valueString", raw)
    raise TransformError(FailureKind.TYPE_MISMATCH, f"unsupported OBX-2 {vtype!r}", ("valueString", raw))


TRANSFORMS = {
    "v2_date": t_v2_date,
    "v2_ts": t_v2_ts,
    "assigning_authority": t_assigning_authority,
    "phone": t_phone,
    "patient_class": t_patient_class,
    "encounter_status": t_encounter_status,
    "ce": t_ce,
    "obx_value": t_obx_value,
}
_WANTS_COMPONENTS = {"ce"}


# --- path setter -------------------------------------------------------------------------------

_TOKEN = re.compile(r"([^.\[]+)(\[(\d*)\])?")


def set_path(obj: dict, path: str, value) -> None:
    """Supports a.b, a[0].b, a[] (append)."""
    if path == "value[x]":
        key, val = value
        obj[key] = val
        return
    parts = path.split(".")
    cur = obj
    for i, part in enumerate(parts):
        name, bracket, idx = _TOKEN.fullmatch(part).groups()
        last = i == len(parts) - 1
        if not bracket:
            if last:
                cur[name] = value
            else:
                cur = cur.setdefault(name, {})
            continue
        lst = cur.setdefault(name, [])
        if idx == "":  # append
            if last:
                lst.append(value)
            else:
                lst.append({})
                cur = lst[-1]
            continue
        j = int(idx)
        while len(lst) <= j:
            lst.append({})
        if last:
            lst[j] = value
        else:
            cur = lst[j]


# --- engine ------------------------------------------------------------------------------------


@cache
def load_template(name: str) -> dict:
    return yaml.safe_load((TEMPLATES / f"{name}.yaml").read_text())


def _source(seg: Segment, ref: str) -> tuple[int, int | None]:
    # "PID-3.4" -> (3, 4); "PID-8" -> (8, None)
    f = ref.split("-", 1)[1]
    n, _, c = f.partition(".")
    return int(n), int(c) if c else None


def apply_template(tpl: dict, seg: Segment, role: str, res: MapResult) -> dict:
    out: dict = {"resourceType": tpl["resource"]}
    for spec in tpl["fields"]:
        target = spec["to"]
        if "const" in spec:
            set_path(out, target, spec["const"])
            continue
        refs = spec["from"] if isinstance(spec["from"], list) else [spec["from"]]
        tname = spec.get("transform")
        values, raw_repr = [], []
        for ref in refs:
            n, c = _source(seg, ref)
            if (isinstance(tname, str) and tname in _WANTS_COMPONENTS) or (tname == "obx_value" and n in (5, 6)):
                v = seg.comps(n)
            else:
                v = seg.get(n, c) if c else seg.get(n)
            values.append(v)
            raw_repr.append(seg.fields[n] if n < len(seg.fields) else "")
            if seg.fields[n:n + 1] and seg.fields[n]:
                res.consumed.add((seg.index, n))
        arg = values if len(refs) > 1 else values[0]
        empty = not any(values) if isinstance(arg, list) and len(refs) > 1 else not arg
        src = ",".join(refs)

        def err(kind, detail, value=None):
            res.errors.append(
                FieldError(str(kind), role, src, seg.index, value if value is not None else "|".join(raw_repr), target, detail)
            )

        if empty and not spec.get("always"):
            if spec.get("required"):
                err(FailureKind.MISSING_REQUIRED, "required source is empty")
            continue
        try:
            if tname is None:
                val = arg
            elif isinstance(tname, dict):
                if arg not in tname["map"]:
                    raise TransformError(FailureKind.FREETEXT_CODED, f"{arg!r} not in value table")
                val = tname["map"][arg]
            else:
                val = TRANSFORMS[tname](arg)
        except TransformError as e:
            err(e.kind, e.detail)
            if e.fallback is None:
                continue
            val = e.fallback
        if isinstance(val, str) and _MOJIBAKE.search(val):
            err(FailureKind.ENCODING, "likely UTF-8 decoded as Latin-1", val)
        set_path(out, target, val)
    return out


def map_message(text: str, msg_id: str | None = None) -> MapResult:
    p: Parsed = parse(text)
    msh = p.first("MSH")
    msg_id = msg_id or (msh.get(10) if msh else "unknown")
    res = MapResult(msg_id, p.msg_type, {})
    res.total_fields = sum(
        1 for s in p.segments if s.id not in ("MSH", "EVN") for n in range(1, len(s.fields)) if s.fields[n]
    )
    res.unmapped = [e for e in p.errors if e["kind"] in ("Z_SEGMENT", "UNKNOWN_SEGMENT")]
    for e in res.unmapped:
        seg = p.segments[e["index"]]
        res.errors.append(
            FieldError(FailureKind.Z_SEGMENT, "?", seg.id, seg.index, "|".join(seg.fields[1:]), "?", "segment has no rule")
        )

    if pid := p.first("PID"):
        res.resources["patient"] = apply_template(load_template("patient"), pid, "patient", res)
    if pv1 := p.first("PV1"):
        res.resources["encounter"] = apply_template(load_template("encounter"), pv1, "encounter", res)
    if obr := p.first("OBR"):
        res.resources["report"] = apply_template(load_template("diagnostic_report"), obr, "report", res)
    last_obs = None
    k = 0
    for seg in p.segments:
        if seg.id == "OBX":
            k += 1
            last_obs = f"obs{k}"
            res.resources[last_obs] = apply_template(load_template("observation"), seg, last_obs, res)
        elif seg.id == "NTE" and last_obs:
            text_ = seg.get(3)
            if text_:
                res.resources[last_obs].setdefault("note", []).append({"text": text_})
                res.consumed.add((seg.index, 3))
    for seg in p.segments:  # set-id fields are structural, count them as consumed
        if seg.id in ("PID", "PV1", "OBR", "OBX", "NTE") and seg.fields[1:2] and seg.fields[1]:
            res.consumed.add((seg.index, 1))
        if seg.id == "OBX" and seg.fields[2:3] and seg.fields[2]:
            res.consumed.add((seg.index, 2))
    return res


def assemble(msg_id: str, msg_type: str, resources: dict[str, dict]) -> dict:
    """Wire references with deterministic urn:uuids and wrap in a transaction Bundle."""
    urls = {role: F.urn(msg_id, role) for role in resources}
    entries = []
    obs_roles = [r for r in resources if r.startswith("obs")]
    for role, r in resources.items():
        r = {**r}
        rt = r["resourceType"]
        if rt != "Patient" and "patient" in urls:
            r["subject"] = {"reference": urls["patient"]}
        if rt in ("Observation", "DiagnosticReport") and "encounter" in urls:
            r["encounter"] = {"reference": urls["encounter"]}
        if rt == "DiagnosticReport":
            r["result"] = [{"reference": urls[o]} for o in obs_roles]
        entries.append((urls[role], r))
    return F.transaction(entries, msg_type)
