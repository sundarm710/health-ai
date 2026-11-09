"""Operation catalog: the pasted form spec as data, plus generic field parsing/checks.

One YAML file per operation (src/fhirlab/clients/nicu/catalog/*.yaml) drives three things:
the Streamlit form, the permission check, and the field-level validation here.
Mapping to FHIR lives in mapping.py (it needs code, not data).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CATALOG_DIR = Path(__file__).parent / "catalog"
ROLES = ("Doctor", "Nurse")

# Unit-level switches that change forms (Form 1: "the unit's ABHA mode changes this form").
UNIT_POLICY = {"abha_required": True}


@dataclass(frozen=True)
class Issue:
    """severity: error blocks Submit, warning does not.
    stage: authz | field | clinical | state | fhir."""

    severity: str
    stage: str
    path: str
    message: str

    def as_dict(self) -> dict:
        return {"severity": self.severity, "stage": self.stage, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str = "text"  # text | longtext | number | single | date | time
    section: str = ""
    required: bool = False
    required_if: dict | None = None
    required_if_policy: str | None = None
    show_if: dict | None = None
    min: float | None = None
    max: float | None = None
    integer: bool = False
    unit: str | None = None
    options: tuple = ()
    options_from: str | None = None  # dynamic options (e.g. "doctors"), supplied at runtime
    other: bool = False  # add an "Other" option
    default: Any = None
    pattern: str | None = None
    pattern_msg: str | None = None
    assumed: str | None = None  # gap in the pasted spec that this repo filled with a default


@dataclass(frozen=True)
class Operation:
    id: str
    form: str
    title: str
    purpose: str
    roles: dict
    needs_patient: bool
    fields: tuple[Field, ...]
    flag: str | None = None

    def allowed(self, role: str) -> bool:
        return bool(self.roles.get(role))

    def field(self, key: str) -> Field:
        return next(f for f in self.fields if f.key == key)


def matches(cond: dict | None, values: dict) -> bool:
    """All keys must match; a list means 'any of'."""
    if not cond:
        return True
    for k, want in cond.items():
        got = values.get(k)
        if got not in (want if isinstance(want, list) else [want]):
            return False
    return True


def visible(f: Field, values: dict) -> bool:
    return matches(f.show_if, values)


def is_required(f: Field, values: dict) -> bool:
    if not visible(f, values):
        return False
    if f.required:
        return True
    if f.required_if is not None and matches(f.required_if, values):
        return True
    return bool(f.required_if_policy and UNIT_POLICY.get(f.required_if_policy))


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Operation]:
    ops = {}
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        d = yaml.safe_load(path.read_text())
        fields = tuple(
            Field(**{**f, "options": tuple(f.get("options", ()))}) for f in d.pop("fields")
        )
        ops[d["id"]] = Operation(fields=fields, **d)
    return ops


def _missing(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def parse_fields(op: Operation, raw: dict, dynamic: dict[str, list[str]] | None = None) -> tuple[dict, list[Issue]]:
    """Coerce raw form values to typed values and apply required/range/option/pattern rules.
    Fields that are not visible (their show_if is false) are dropped."""
    dynamic = dynamic or {}
    issues: list[Issue] = []
    parsed: dict[str, Any] = {}
    # visibility depends on other raw values, so evaluate against the raw dict
    for f in op.fields:
        if not visible(f, raw):
            if not _missing(raw.get(f.key)):
                issues.append(Issue("warning", "field", f.key, f"'{f.label}' is not applicable here and was ignored"))
            continue
        v = raw.get(f.key)
        if _missing(v):
            if is_required(f, raw):
                issues.append(Issue("error", "field", f.key, f"{f.label} is required"))
            continue
        try:
            parsed[f.key] = _coerce(f, v, dynamic)
        except ValueError as e:
            issues.append(Issue("error", "field", f.key, f"{f.label}: {e}"))
    unknown = set(raw) - {f.key for f in op.fields}
    for k in sorted(unknown):
        issues.append(Issue("error", "field", k, f"Unknown field '{k}' for {op.id}"))
    return parsed, issues


def _coerce(f: Field, v: Any, dynamic: dict[str, list[str]]) -> Any:
    if f.type == "number":
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"'{v}' is not a number") from None
        if f.integer:
            if n != int(n):
                raise ValueError("must be a whole number")
            n = int(n)
        if f.min is not None and n < f.min or f.max is not None and n > f.max:
            raise ValueError(f"{n} is outside the allowed range {f.min}–{f.max}" + (f" {f.unit}" if f.unit else ""))
        return n
    if f.type == "single":
        allowed = list(f.options) + list(dynamic.get(f.options_from or "", [])) + (["Other"] if f.other else [])
        if v not in allowed:
            raise ValueError(f"'{v}' is not one of {allowed}")
        return v
    if f.type == "date":
        try:
            return date.fromisoformat(str(v)).isoformat()
        except ValueError:
            raise ValueError(f"'{v}' is not a date (YYYY-MM-DD)") from None
    if f.type == "time":
        try:
            return time.fromisoformat(str(v)).strftime("%H:%M")
        except ValueError:
            raise ValueError(f"'{v}' is not a time (HH:MM)") from None
    s = str(v).strip()
    if f.pattern and not re.match(f.pattern, s):
        raise ValueError(f.pattern_msg or f"does not match {f.pattern}")
    return s
