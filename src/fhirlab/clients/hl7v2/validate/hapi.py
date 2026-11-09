"""HAPI FHIR $validate wrapper with a content-hash disk cache.

Only error/fatal issues count as invalid. Warnings (unknown code systems, missing narrative,
best-practice hints) are noise for repair purposes; note that this means a local code or a
wrong LOINC passes validation. Validity is not correctness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from fhirlab.core.client import FhirClient

CACHE = Path(".cache/validate")


@dataclass
class Issue:
    severity: str
    path: str
    message: str


def _issues(oo: dict) -> list[Issue]:
    out = []
    for i in oo.get("issue", []):
        path = (i.get("expression") or i.get("location") or [""])[0]
        out.append(Issue(i.get("severity", ""), path, i.get("diagnostics", "")))
    return out


def validate(resource: dict, *, client: httpx.Client | None = None) -> list[Issue]:
    """Validate a resource or Bundle. Returns error/fatal issues only (empty list == valid)."""
    body = json.dumps(resource, sort_keys=True)
    key = hashlib.sha256(body.encode()).hexdigest()
    cached = CACHE / f"{key}.json"
    if cached.exists():
        issues = [Issue(**d) for d in json.loads(cached.read_text())]
    else:
        issues = _issues({"issue": FhirClient(client=client or httpx.Client(timeout=120)).validate(resource)})  # the one HAPI client
        CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps([asdict(i) for i in issues]))
    return [i for i in issues if i.severity in ("error", "fatal")]
