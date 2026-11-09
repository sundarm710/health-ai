"""Minimal HL7v2 text primitives shared by emitter, corruptor and parser.

A segment is a list[str] of *raw* (still-escaped) fields where seg[n] is field n and seg[0] is
the segment id. For MSH we insert "|" at index 1 so MSH-n is also seg[n].
"""

from __future__ import annotations

import re

FS, CS, RS, EC, SS = "|", "^", "~", "\\", "&"
ENCODING_CHARS = "^~\\&"
SEG_SEP = "\r"

_ESC = [(EC, "\\E\\"), (FS, "\\F\\"), (CS, "\\S\\"), (RS, "\\R\\"), (SS, "\\T\\")]


def escape(s: str) -> str:
    for ch, rep in _ESC:
        s = s.replace(ch, rep)
    return s


def unescape(s: str) -> str:
    for ch, rep in reversed(_ESC):
        s = s.replace(rep, ch)
    return s


def comp(*parts: str | None) -> str:
    """Join components, escaping each and trimming trailing empties."""
    vals = [escape(p) if p else "" for p in parts]
    while vals and not vals[-1]:
        vals.pop()
    return CS.join(vals)


def segment(seg_id: str, fields: dict[int, str]) -> list[str]:
    n = max(fields) if fields else 0
    seg = [seg_id] + [""] * n
    for i, v in fields.items():
        seg[i] = v
    return seg


def split_segments(text: str) -> list[list[str]]:
    segs = []
    for line in re.split(r"\r\n|\r|\n", text):
        if not line.strip():
            continue
        f = line.split(FS)
        if f[0] == "MSH":
            f.insert(1, FS)
        segs.append(f)
    return segs


def join_segments(segs: list[list[str]]) -> str:
    lines = []
    for s in segs:
        if s[0] == "MSH":
            s = [s[0]] + s[2:]
        lines.append(FS.join(s))
    return SEG_SEP.join(lines) + SEG_SEP


def field(seg: list[str], n: int) -> str:
    return seg[n] if n < len(seg) else ""


def set_field(seg: list[str], n: int, value: str) -> None:
    while len(seg) <= n:
        seg.append("")
    seg[n] = value


def components(raw: str) -> list[str]:
    """First repetition only, split into unescaped components."""
    return [unescape(c) for c in raw.split(RS)[0].split(CS)]


# --- timestamps -----------------------------------------------------------------------------

_FHIR_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?)?(Z|[+-]\d{2}:\d{2})?$")
_V2_TS = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.\d+)?([+-]\d{4})?$")


def fhir_to_v2_ts(dt: str | None) -> str:
    if not dt:
        return ""
    m = _FHIR_DT.match(dt)
    if not m:
        raise ValueError(f"not a FHIR dateTime: {dt}")
    y, mo, d, hh, mm, ss, tz = m.groups()
    out = y + mo + d + (hh or "") + (mm or "") + (ss or "")
    if tz and hh:
        out += "+0000" if tz == "Z" else tz.replace(":", "")
    return out


def v2_ts_to_fhir(ts: str) -> str:
    """Strict v2 TS parser. Raises ValueError on anything non-conformant (that's the point)."""
    m = _V2_TS.match(ts.strip())
    if not m:
        raise ValueError(f"not a v2 TS: {ts!r}")
    y, mo, d, hh, mm, ss, tz = m.groups()
    if not (mo and d):
        raise ValueError(f"TS lacks day precision: {ts!r}")
    out = f"{y}-{mo}-{d}"
    if hh:
        out += f"T{hh}:{mm or '00'}:{ss or '00'}"
        if tz:
            out += f"{tz[:3]}:{tz[3:]}"
    return out
