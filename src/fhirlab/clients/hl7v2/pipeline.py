"""Pipeline configurations. Each takes raw v2 text and returns an Outcome.

Phase 2 ships `rules_only`; agent configs (mapper, repair, terminology, reconciler) plug in here.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from fhirlab.clients.hl7v2.rules.mapper import map_message
from fhirlab.clients.hl7v2.validate.hapi import validate


@dataclass
class Outcome:
    bundle: dict | None
    path: str  # "fast" | "agent" | "escalated"
    escalated: bool = False
    reason: str = ""
    valid: bool = False
    validator_errors: int = 0
    coverage: float = 0.0
    rule_errors: list[str] = field(default_factory=list)  # FailureKinds the rules layer flagged
    field_errors: list[dict] = field(default_factory=list)  # full FieldError detail (artifact only)
    issues: list[dict] = field(default_factory=list)  # validator error/fatal issues (artifact only)
    iterations: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0


def rules_only(text: str, msg_id: str) -> Outcome:
    t0 = time.perf_counter()
    res = map_message(text, msg_id)
    bundle = res.bundle()
    issues = validate(bundle)
    # Rules-only has nowhere to send a flagged message except a human, so a dirty message is
    # "escalated". It still emits its best bundle so we can score what it would have written.
    return Outcome(
        bundle=bundle,
        path="fast" if res.clean and not issues else "escalated",
        escalated=not (res.clean and not issues),
        reason=",".join(sorted({e.kind for e in res.errors})) or ("INVALID" if issues else ""),
        valid=not issues,
        validator_errors=len(issues),
        coverage=res.coverage,
        rule_errors=sorted({e.kind for e in res.errors}),
        field_errors=res.errors_dicts(),
        issues=[asdict(i) for i in issues],
        latency_s=time.perf_counter() - t0,
    )


CONFIGS = {"rules_only": rules_only}
