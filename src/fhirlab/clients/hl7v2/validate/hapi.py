"""HAPI FHIR $validate wrapper with a content-hash disk cache.

Only error/fatal issues count as invalid. Warnings (unknown code systems, missing narrative,
best-practice hints) are noise for repair purposes; note that this means a local code or a
wrong LOINC passes validation. Validity is not correctness.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

BASE = os.environ.get("HAPI_BASE", "http://localhost:8080/fhir")
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
        rt = resource.get("resourceType", "Bundle")
        c = client or httpx.Client(timeout=120)
        r = c.post(f"{BASE}/{rt}/$validate", content=body, headers={"Content-Type": "application/fhir+json"})
        issues = _issues(r.json())
        CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps([asdict(i) for i in issues]))
    return [i for i in issues if i.severity in ("error", "fatal")]
