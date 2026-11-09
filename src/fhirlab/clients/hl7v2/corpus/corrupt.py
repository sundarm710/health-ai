"""Seeded corruption generator over clean v2 text.

Nesting: each corruption type draws u ~ U(0,1) from a per-message RNG that does not depend on
the level, and fires when u < level. So the corruptions at level 0.4 are a subset of those at
0.6, and accuracy-vs-level curves reflect difficulty rather than resampling noise.

Every change is recorded in the manifest, so eval can break accuracy down by corruption type.
"""

from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass

from fhirlab.clients.hl7v2.v2 import comp, components, escape, field, join_segments, set_field, split_segments

TYPES = [
    "Z_SEGMENT_MOVE",
    "LOCAL_LAB_CODE",
    "FREETEXT_IN_CODED",
    "DROP_REQUIRED_COMPONENT",
    "MANGLED_UNITS",
    "MIXED_DATE_FORMAT",
    "TRUNCATED_NAME",
    "BAD_ENCODING",
]

UNIT_MANGLES = {
    "mg/dL": ["mg/dl", "MG/DL", "mg per dL", "mg%"],
    "mmol/L": ["mmol/l", "MMOL/L", "mM", "mmol per L"],
    "g/dL": ["g/dl", "gm/dL", "G/DL"],
    "10*3/uL": ["K/uL", "10^3/uL", "thou/uL"],
    "10*6/uL": ["M/uL", "10^6/uL", "mill/uL"],
    "%": ["percent", "pct"],
    "fL": ["fl", "FL", "um3"],
    "pg": ["PG", "picogram"],
    "U/L": ["u/l", "IU/L", "units/L"],
    "g/L": ["g/l", "G/L"],
    "mL/min": ["ml/min", "ML/MIN"],
    "mL/min/{1.73_m2}": ["mL/min/1.73m2", "ml/min/1.73 m2"],
}

FREETEXT = {("PID", 8): {"M": "Male", "F": "Female"}, ("PV1", 2): {"I": "Inpatient", "E": "Emergency", "O": "Outpatient"}}


@dataclass
class Change:
    type: str
    segment: str
    seg_index: int  # position in message
    field: str  # e.g. "PID-13" or "OBX-3.3"
    before: str
    after: str


class _Msg:
    def __init__(self, text: str):
        self.segs = split_segments(text)
        self.changes: list[Change] = []
        self.touched: set[tuple[int, int]] = set()

    def find(self, seg_id: str) -> list[int]:
        return [i for i, s in enumerate(self.segs) if s[0] == seg_id]

    def edit(self, ctype: str, i: int, n: int, new: str, label: str | None = None) -> None:
        seg = self.segs[i]
        before = field(seg, n)
        set_field(seg, n, new)
        self.touched.add((i, n))
        self.changes.append(Change(ctype, seg[0], i, label or f"{seg[0]}-{n}", before, new))


# --- corruption functions: (msg, rng, ctx) -> None ------------------------------------------


def _z_segment_move(m: _Msg, rng: random.Random, ctx: dict) -> None:
    opts = [("PID", 13, "ZPI"), ("PV1", 19, "ZPV")]
    seg_id, n, z = rng.choice(opts)
    i = m.find(seg_id)[0]
    val = field(m.segs[i], n)
    if not val:
        return
    m.edit("Z_SEGMENT_MOVE", i, n, "")
    m.segs.append([z, "1", val])
    m.changes.append(Change("Z_SEGMENT_MOVE", z, len(m.segs) - 1, f"{z}-2", "", val))


def _local_lab_code(m: _Msg, rng: random.Random, ctx: dict) -> None:
    obx = m.find("OBX")
    if not obx:
        return
    hits = [i for i in obx if rng.random() < 0.5] or [rng.choice(obx)]
    for i in hits:
        if (i, 3) in m.touched:
            continue
        loinc = components(field(m.segs[i], 3))[0]
        local = ctx["local_codes"].get(loinc)
        if local:
            m.edit("LOCAL_LAB_CODE", i, 3, comp(local["code"], local["display"], "L"))


def _freetext_in_coded(m: _Msg, rng: random.Random, ctx: dict) -> None:
    sites = []
    for (seg_id, n), table in FREETEXT.items():
        for i in m.find(seg_id):
            if field(m.segs[i], n) in table:
                sites.append((i, n, table[field(m.segs[i], n)]))
    for i in m.find("OBX"):
        c = components(field(m.segs[i], 3))
        sites.append((i, 3, c[1] if len(c) > 1 else c[0]))  # display text only, no code
        if field(m.segs[i], 2) == "CE":
            v = components(field(m.segs[i], 5))
            sites.append((i, 5, v[1] if len(v) > 1 else v[0]))
    sites = [s for s in sites if (s[0], s[1]) not in m.touched]
    if sites:
        i, n, text = rng.choice(sites)
        m.edit("FREETEXT_IN_CODED", i, n, escape(text))


def _drop_required_component(m: _Msg, rng: random.Random, ctx: dict) -> None:
    sites = []
    pid = m.find("PID")[0]
    sites.append(("PID-3.4", pid, 3, lambda c: c[:3] + [""] + c[4:]))
    for i in m.find("OBX"):
        sites.append(("OBX-3.3", i, 3, lambda c: c[:2]))
        sites.append(("OBX-11", i, 11, None))
    sites = [s for s in sites if (s[1], s[2]) not in m.touched]
    label, i, n, fn = rng.choice(sites)
    if fn is None:
        m.edit("DROP_REQUIRED_COMPONENT", i, n, "", label)
    else:
        c = components(field(m.segs[i], n))
        m.edit("DROP_REQUIRED_COMPONENT", i, n, comp(*fn(c)), label)


def _mangled_units(m: _Msg, rng: random.Random, ctx: dict) -> None:
    nm = [i for i in m.find("OBX") if field(m.segs[i], 2) == "NM" and field(m.segs[i], 6)]
    if not nm:
        return
    hits = [i for i in nm if rng.random() < 0.5] or [rng.choice(nm)]
    for i in hits:
        unit = components(field(m.segs[i], 6))[0]
        opts = UNIT_MANGLES.get(unit) or [unit.lower() if unit.lower() != unit else unit.upper()]
        new = rng.choice(opts)
        if new != unit:
            m.edit("MANGLED_UNITS", i, 6, escape(new))


_TS = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?([+-]\d{4})?$")
_MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _reformat(ts: str, rng: random.Random) -> str:
    y, mo, d, hh, mm, ss, tz = _TS.match(ts).groups()
    fmts = [f"{mo}/{d}/{y}", f"{y}-{mo}-{d}", f"{d}-{_MON[int(mo) - 1]}-{y}"]
    if hh:
        fmts += [f"{y}-{mo}-{d} {hh}:{mm}", f"{mo}/{d}/{y} {hh}:{mm}:{ss}"]
    return rng.choice(fmts)


def _mixed_date_format(m: _Msg, rng: random.Random, ctx: dict) -> None:
    sites = [(m.find("PID")[0], 7)]
    sites += [(i, 44) for i in m.find("PV1")]
    sites += [(i, 7) for i in m.find("OBR")] + [(i, 14) for i in m.find("OBX")]
    sites = [(i, n) for i, n in sites if _TS.match(field(m.segs[i], n)) and (i, n) not in m.touched]
    for i, n in rng.sample(sites, k=min(len(sites), rng.randint(1, 3))):
        m.edit("MIXED_DATE_FORMAT", i, n, escape(_reformat(field(m.segs[i], n), rng)))


def _truncated_name(m: _Msg, rng: random.Random, ctx: dict) -> None:
    i = m.find("PID")[0]
    c = components(field(m.segs[i], 5))
    if len(c[0]) > 6:
        c[0] = c[0][: rng.randint(4, 6)]
        m.edit("TRUNCATED_NAME", i, 5, comp(*c), "PID-5.1")


def _bad_encoding(m: _Msg, rng: random.Random, ctx: dict) -> None:
    i = m.find("PID")[0]
    for n in (5, 11):
        raw = field(m.segs[i], n)
        if any(ord(ch) > 127 for ch in raw) and (i, n) not in m.touched:
            m.edit("BAD_ENCODING", i, n, raw.encode("utf-8").decode("latin-1"))


FUNCS = {
    "Z_SEGMENT_MOVE": _z_segment_move,
    "LOCAL_LAB_CODE": _local_lab_code,
    "FREETEXT_IN_CODED": _freetext_in_coded,
    "DROP_REQUIRED_COMPONENT": _drop_required_component,
    "MANGLED_UNITS": _mangled_units,
    "MIXED_DATE_FORMAT": _mixed_date_format,
    "TRUNCATED_NAME": _truncated_name,
    "BAD_ENCODING": _bad_encoding,
}


def corrupt(text: str, level: float, seed: str, ctx: dict) -> tuple[str, list[dict]]:
    """Apply corruptions. `seed` should be the msg_id (level-independent, see module doc)."""
    rng = random.Random(seed)
    m = _Msg(text)
    for ctype in TYPES:
        u = rng.random()
        sub = random.Random(rng.random())  # drawn every time so the stream never shifts
        if u < level:
            FUNCS[ctype](m, sub, ctx)
    return join_segments(m.segs), [asdict(c) for c in m.changes]


# --- local lab code table (the terminology agent's hidden ground truth) ---------------------

_ABBREV = [
    (r"\[[^\]]*\]", ""),
    (r"\bin Serum or Plasma\b", "SER/PL"),
    (r"\bin Blood\b", "BLD"),
    (r"\bin Urine\b", "UR"),
    (r"\bby Automated count\b", "AUTO"),
    (r"\bby Test strip\b", "STRIP"),
    (r"\bRed Blood Cells\b", "RBC"),
    (r"\s+", " "),
]


def local_code_table(loinc_display: dict[str, str], seed: int = 7) -> dict[str, dict]:
    rng = random.Random(seed)
    table, used = {}, set()
    for loinc, display in sorted(loinc_display.items()):
        text = display
        for pat, rep in _ABBREV:
            text = re.sub(pat, rep, text)
        text = text.strip().upper()[:28]
        stem = re.sub(r"[^A-Z]", "", text)[:4] or "LAB"
        code = f"LAB-{stem}-{rng.randint(10, 99)}"
        while code in used:
            code = f"LAB-{stem}-{rng.randint(10, 99)}"
        used.add(code)
        table[loinc] = {"code": code, "display": text}
    return table
