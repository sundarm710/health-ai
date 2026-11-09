"""MessageFacts -> clean, conformant HL7v2.5.1 text."""

from __future__ import annotations

from fhirlab.corpus.facts import MessageFacts
from fhirlab.v2 import ENCODING_CHARS, comp, escape, fhir_to_v2_ts, join_segments, segment

ASSIGNING_AUTHORITY = "HOSP"
CLASS_TO_PV1 = {"IMP": "I", "EMER": "E", "AMB": "O"}
GENDER_TO_V2 = {"male": "M", "female": "F", "other": "O", "unknown": "U"}


def _msh(f: MessageFacts) -> list[str]:
    event = f.msg_type.split("^")[1]
    structure = {"A01": "ADT_A01", "A08": "ADT_A01", "R01": "ORU_R01"}[event]
    return segment(
        "MSH",
        {
            1: "|",
            2: ENCODING_CHARS,
            3: "SYNLAB" if event == "R01" else "SYNADT",
            4: "SYNHOSP",
            5: "H2F",
            6: "H2F",
            7: fhir_to_v2_ts(f.timestamp),
            9: comp(*f.msg_type.split("^"), structure),
            10: f.msg_id,
            11: "P",
            12: "2.5.1",
            18: "UNICODE UTF-8",
        },
    )


def _pid(f: MessageFacts) -> list[str]:
    p = f.patient
    return segment(
        "PID",
        {
            1: "1",
            3: comp(p.mrn, "", "", ASSIGNING_AUTHORITY, "MR"),
            5: comp(p.family, *(p.given + [""] * (2 - len(p.given)))),
            7: p.birth_date.replace("-", ""),
            8: GENDER_TO_V2[p.gender],
            11: comp(p.line, "", p.city, p.state, p.postal, "US"),
            13: escape(p.phone),
        },
    )


def _pv1(f: MessageFacts) -> list[str]:
    e = f.encounter
    return segment(
        "PV1",
        {
            1: "1",
            2: CLASS_TO_PV1[e.cls],
            19: escape(e.visit_number),
            44: fhir_to_v2_ts(e.start),
            45: fhir_to_v2_ts(e.end),
        },
    )


def emit(f: MessageFacts) -> str:
    segs = [_msh(f)]
    event = f.msg_type.split("^")[1]
    if event in ("A01", "A08"):
        segs.append(segment("EVN", {1: event, 2: fhir_to_v2_ts(f.timestamp)}))
    segs += [_pid(f), _pv1(f)]
    if f.report:
        r = f.report
        segs.append(
            segment(
                "OBR",
                {
                    1: "1",
                    3: escape(r.filler_id),
                    4: comp(r.loinc, r.display, "LN"),
                    7: fhir_to_v2_ts(r.effective),
                    22: fhir_to_v2_ts(r.issued),
                    25: "F",
                },
            )
        )
        for i, o in enumerate(r.observations, 1):
            if o.value_type == "NM":
                value, units = f"{o.value:g}", comp(o.unit)
            else:
                value, units = comp(o.value_code, o.value_display, "SCT"), ""
            segs.append(
                segment(
                    "OBX",
                    {
                        1: str(i),
                        2: o.value_type,
                        3: comp(o.loinc, o.display, "LN"),
                        5: value,
                        6: units,
                        11: "F",
                        14: fhir_to_v2_ts(o.effective),
                    },
                )
            )
            if o.note:
                segs.append(segment("NTE", {1: "1", 3: escape(o.note)}))
    return join_segments(segs)
