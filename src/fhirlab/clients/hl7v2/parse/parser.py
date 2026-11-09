"""Tolerant v2 parser: never raises. Structural problems come back as data for triage."""

from __future__ import annotations

from dataclasses import dataclass, field

from fhirlab.clients.hl7v2.v2 import components, split_segments
from fhirlab.clients.hl7v2.v2 import field as raw_field

KNOWN = {"MSH", "EVN", "PID", "PV1", "OBR", "OBX", "NTE"}


@dataclass
class Segment:
    id: str
    index: int  # position in message
    fields: list[str]  # raw, still escaped; fields[n] is field n

    def get(self, n: int, comp: int | None = None) -> str:
        raw = raw_field(self.fields, n)
        if comp is None:
            return components(raw)[0] if raw else ""
        c = components(raw)
        return c[comp - 1] if len(c) >= comp else ""

    def comps(self, n: int) -> list[str]:
        raw = raw_field(self.fields, n)
        return components(raw) if raw else []

    def to_dict(self) -> dict:
        """Compact {"PID-3": [...components]} view used in agent prompts."""
        return {f"{self.id}-{n}": self.comps(n) for n in range(1, len(self.fields)) if self.fields[n]}


@dataclass
class Parsed:
    msg_type: str
    segments: list[Segment]
    errors: list[dict] = field(default_factory=list)

    def all(self, seg_id: str) -> list[Segment]:
        return [s for s in self.segments if s.id == seg_id]

    def first(self, seg_id: str) -> Segment | None:
        s = self.all(seg_id)
        return s[0] if s else None


def parse(text: str) -> Parsed:
    errors: list[dict] = []
    try:
        raw = split_segments(text)
    except Exception as e:  # pragma: no cover - split is total, but stay defensive
        return Parsed("", [], [{"kind": "UNPARSEABLE", "detail": str(e)}])
    segs = [Segment(r[0], i, r) for i, r in enumerate(raw)]
    if not segs or segs[0].id != "MSH":
        errors.append({"kind": "NO_MSH", "detail": "first segment is not MSH"})
    msh = segs[0] if segs and segs[0].id == "MSH" else None
    msg_type = "^".join(msh.comps(9)[:2]) if msh else ""
    for s in segs:
        if s.id.startswith("Z"):
            errors.append({"kind": "Z_SEGMENT", "segment": s.id, "index": s.index})
        elif s.id not in KNOWN:
            errors.append({"kind": "UNKNOWN_SEGMENT", "segment": s.id, "index": s.index})
    return Parsed(msg_type, segs, errors)
