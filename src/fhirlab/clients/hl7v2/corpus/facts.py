"""Canonical facts: the subset of Synthea FHIR that our three v2 message types can carry.

Both the v2 emitter and the golden-bundle builder read from these dataclasses, so the golden
bundle is by construction the *projection* of Synthea onto what v2 transports. Scoring against
full Synthea would put an unexplained ceiling on recall.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class PatientFacts:
    mrn: str
    family: str
    given: list[str]
    birth_date: str  # YYYY-MM-DD
    gender: str  # male | female
    line: str
    city: str
    state: str
    postal: str
    phone: str


@dataclass
class EncounterFacts:
    visit_number: str
    cls: str  # v3 ActCode: AMB | EMER | IMP
    start: str  # FHIR dateTime
    end: str | None


@dataclass
class ObservationFacts:
    loinc: str
    display: str
    value_type: str  # NM | CE
    value: float | None = None
    unit: str | None = None  # UCUM code
    value_code: str | None = None  # SNOMED for CE
    value_display: str | None = None
    effective: str = ""
    note: str | None = None


@dataclass
class ReportFacts:
    filler_id: str
    loinc: str
    display: str
    effective: str
    issued: str
    observations: list[ObservationFacts] = field(default_factory=list)


@dataclass
class MessageFacts:
    msg_id: str
    patient_id: str
    msg_type: str  # ADT^A01 | ADT^A08 | ORU^R01
    timestamp: str
    patient: PatientFacts
    encounter: EncounterFacts
    report: ReportFacts | None = None
    tags: list[str] = field(default_factory=list)  # e.g. PROMPT_INJECTION_NTE

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> MessageFacts:
        rep = d.get("report")
        if rep:
            rep = ReportFacts(**{**rep, "observations": [ObservationFacts(**o) for o in rep["observations"]]})
        return MessageFacts(
            **{**d, "patient": PatientFacts(**d["patient"]), "encounter": EncounterFacts(**d["encounter"]), "report": rep}
        )
