"""Pure transforms over the repo's DataFrames. This is where "what does a row *mean*" lives."""

from __future__ import annotations

import json

import pandas as pd

from h2f.dbview.repo import DbRepo

TIMELINE_COLS = ["when", "res_type", "fhir_id", "res_ver", "kind", "summary", "request_id", "res_id"]


def summarize(res_type: str, body: str | None) -> str:
    """One line a human can scan: 'Body weight 1.4 kg', 'Encounter in-progress', 'Procedure: Central line (in-progress)'."""
    if not isinstance(body, str) or not body:  # None or NaN (pandas) = a delete marker has no body
        return "(deleted)"
    d = json.loads(body)
    code = d.get("code") or d.get("type") or {}
    code = code[0] if isinstance(code, list) and code else code
    label = (code.get("text") or (code.get("coding") or [{}])[0].get("display") or "") if isinstance(code, dict) else ""
    status = d.get("status", "")
    if res_type == "Observation":
        vq = d.get("valueQuantity")
        val = f"{vq.get('value')} {vq.get('unit', '')}".strip() if vq else d.get("valueString") or d.get("valueCodeableConcept", {}).get("text", "")
        return f"{label or 'Observation'} {val}".strip()
    if res_type == "Patient":
        n = (d.get("name") or [{}])[0]
        return " ".join([*n.get("given", []), n.get("family", "")]).strip() or "Patient"
    if res_type == "Provenance":
        acts = d.get("activity", {}).get("coding", [{}])[0].get("code", "")
        return f"Provenance {acts} → {len(d.get('target', []))} target(s)"
    if res_type == "RelatedPerson":
        return f"RelatedPerson {(d.get('relationship') or [{}])[0].get('coding', [{}])[0].get('display', '')}".strip()
    return f"{label or res_type}{f' ({status})' if status else ''}"


def patient_timeline(repo: DbRepo, patient_pid: int) -> pd.DataFrame:
    """Every stored version of everything in the patient's compartment, newest last, with readable summaries."""
    ev = repo.patient_events(patient_pid)
    if ev.empty:
        return pd.DataFrame(columns=TIMELINE_COLS)
    ev = ev.copy()
    ev["when"] = pd.to_datetime(ev["res_updated"], utc=True)
    ev["kind"] = ev.apply(lambda r: "deleted" if r["encoding"] == "DEL" else "created" if r["res_ver"] == 1 else "updated", axis=1)
    ev["summary"] = [summarize(t, b) for t, b in zip(ev["res_type"], ev["body"], strict=True)]
    ev["request_id"] = ev["request_id"].fillna("")
    out = ev[[*TIMELINE_COLS[:-1], "res_id"]].copy()
    out.attrs["bodies"] = {f"{int(r)}:{int(v)}": (b if isinstance(b, str) else None) for r, v, b in zip(ev["res_id"], ev["res_ver"], ev["body"], strict=True)}
    return out


def _flat(o, prefix="") -> dict:
    if isinstance(o, dict):
        return {k2: v2 for k, v in o.items() for k2, v2 in _flat(v, f"{prefix}.{k}" if prefix else k).items()}
    if isinstance(o, list):
        return {k2: v2 for i, v in enumerate(o) for k2, v2 in _flat(v, f"{prefix}[{i}]").items()}
    return {prefix: o}


def diff_versions(before: str | None, after: str | None) -> pd.DataFrame:
    """Field-level change between two stored versions: path, before, after, change."""
    a = _flat(json.loads(before)) if isinstance(before, str) else {}
    b = _flat(json.loads(after)) if isinstance(after, str) else {}
    rows = [{"path": k, "before": a.get(k), "after": b.get(k),
             "change": "added" if k not in a else "removed" if k not in b else "changed"}
            for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k) or (k in a) != (k in b)]
    return pd.DataFrame(rows, columns=["path", "before", "after", "change"])


def link_heatmap(repo: DbRepo) -> pd.DataFrame:
    """Same as repo.link_matrix; kept as a seam so aggregation can move SQL<->Python without touching a UI."""
    return repo.link_matrix()
