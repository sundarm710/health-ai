"""Field-level scoring of a predicted Bundle against the golden Bundle.

flatten: resource -> multiset of (path, normalized value); list indices dropped, references
resolved to "<Type>|<business key>" so we check that an Observation points at the *right*
patient rather than comparing random urn:uuids.
align:   pair predicted/golden resources per type (Hungarian on overlap for Observations).
score:   TP/FP/FN split into critical vs cosmetic, plus "wrong" = a critical path where both
         sides have a value and they differ (a confident error, worse than an omission).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

import numpy as np
from scipy.optimize import linear_sum_assignment

SKIP_KEYS = {"id", "meta", "text"}
CRITICAL = [
    r"\.identifier\.(value|system)$",
    r"^Patient\.(birthDate|gender|name\.family)$",
    r"\.status$",
    r"^Encounter\.(class\.(code|system)|period\.(start|end))$",
    r"\.code\.coding\.(code|system)$",
    r"\.value(Quantity\.(value|code|system)|CodeableConcept\.coding\.(code|system)|String)$",
    r"\.(effectiveDateTime|issued)$",
    r"\.(subject|encounter|result)\.reference$",
]
_CRIT = [re.compile(p) for p in CRITICAL]
_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$")


def is_critical(path: str) -> bool:
    return any(p.search(path) for p in _CRIT)


def _norm(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    if isinstance(v, str):
        s = v.strip()
        if _DT.match(s):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        return s
    return v


def business_key(r: dict) -> str:
    rt = r.get("resourceType", "?")
    ids = r.get("identifier") or []
    if ids and isinstance(ids[0], dict) and ids[0].get("value"):
        return f"{rt}|{ids[0]['value']}"
    if rt == "Observation":
        codings = (r.get("code") or {}).get("coding") or [{}]
        return f"{rt}|{codings[0].get('code') or (r.get('code') or {}).get('text', '?')}"
    return rt


def flatten(r: dict, refs: dict[str, str]) -> Counter:
    out: Counter = Counter()
    rt = r.get("resourceType", "?")

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in SKIP_KEYS or k == "resourceType":
                    continue
                if k == "reference" and isinstance(v, str):
                    out[(f"{path}.reference", refs.get(v, "UNRESOLVED"))] += 1
                    continue
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for x in node:
                walk(x, path)
        elif node is not None and node != "":
            out[(path, _norm(node))] += 1

    walk(r, rt)
    return out


def _resources(bundle: dict) -> tuple[list[dict], dict[str, str]]:
    entries = bundle.get("entry", []) if bundle else []
    res = [e["resource"] for e in entries if "resource" in e]
    refs = {e["fullUrl"]: business_key(e["resource"]) for e in entries if "fullUrl" in e and "resource" in e}
    return res, refs


def _align(pred: list[Counter], gold: list[Counter]) -> list[tuple[int | None, int | None]]:
    if not pred or not gold:
        return [(i, None) for i in range(len(pred))] + [(None, j) for j in range(len(gold))]
    cost = np.array([[-sum((p & g).values()) for g in gold] for p in pred])
    ri, ci = linear_sum_assignment(cost)
    pairs = list(zip(ri.tolist(), ci.tolist()))
    pairs += [(i, None) for i in range(len(pred)) if i not in set(ri)]
    pairs += [(None, j) for j in range(len(gold)) if j not in set(ci)]
    return pairs


def score(pred_bundle: dict | None, gold_bundle: dict) -> dict:
    """Returns flat counts: {tp_crit, fp_crit, fn_crit, wrong_crit, tp_cos, fp_cos, fn_cos,
    and per-type tp/fp/fn: '<Type>.tp' ...}."""
    pred, prefs = _resources(pred_bundle or {})
    gold, grefs = _resources(gold_bundle)
    c: Counter = Counter()
    types = {r["resourceType"] for r in pred + gold}
    for t in types:
        pf = [flatten(r, prefs) for r in pred if r["resourceType"] == t]
        gf = [flatten(r, grefs) for r in gold if r["resourceType"] == t]
        for i, j in _align(pf, gf):
            p = pf[i] if i is not None else Counter()
            g = gf[j] if j is not None else Counter()
            tp, fp, fn = p & g, p - g, g - p
            wrong_paths = {k[0] for k in fp} & {k[0] for k in fn}
            for bucket, cnt in (("tp", tp), ("fp", fp), ("fn", fn)):
                for (path, _), n in cnt.items():
                    kind = "crit" if is_critical(path) else "cos"
                    c[f"{bucket}_{kind}"] += n
                    c[f"{t}.{bucket}"] += n
            c["wrong_crit"] += sum(n for (path, _), n in fp.items() if path in wrong_paths and is_critical(path))
    return dict(c)


def diff(pred_bundle: dict | None, gold_bundle: dict) -> list[dict]:
    """Field-level rows for review. Same flatten/align as score(); every predicted and golden
    value lands in exactly one row: match | wrong (pred and gold differ at the same path) |
    missing (gold only) | extra (pred only)."""
    pred, prefs = _resources(pred_bundle or {})
    gold, grefs = _resources(gold_bundle)
    rows: list[dict] = []
    for t in sorted({r["resourceType"] for r in pred + gold}):
        pr = [r for r in pred if r["resourceType"] == t]
        gr = [r for r in gold if r["resourceType"] == t]
        pf, gf = [flatten(r, prefs) for r in pr], [flatten(r, grefs) for r in gr]
        for i, j in _align(pf, gf):
            p = pf[i] if i is not None else Counter()
            g = gf[j] if j is not None else Counter()
            key = business_key(gr[j]) if j is not None else business_key(pr[i])
            fp, fn = p - g, g - p

            def row(path, status, pv=None, gv=None):
                rows.append(
                    {"resource": t, "key": key, "path": path, "status": status, "critical": is_critical(path),
                     "pred": None if pv is None else str(pv), "gold": None if gv is None else str(gv)}
                )

            for (path, v), n in sorted((p & g).items(), key=str):
                for _ in range(n):
                    row(path, "match", v, v)
            by_path: dict[str, tuple[list, list]] = {}
            for (path, v), n in fp.items():
                by_path.setdefault(path, ([], []))[0].extend([v] * n)
            for (path, v), n in fn.items():
                by_path.setdefault(path, ([], []))[1].extend([v] * n)
            for path, (pvs, gvs) in sorted(by_path.items()):
                pvs, gvs = sorted(pvs, key=str), sorted(gvs, key=str)
                for pv, gv in zip(pvs, gvs):
                    row(path, "wrong", pv, gv)
                for pv in pvs[len(gvs):]:
                    row(path, "extra", pv, None)
                for gv in gvs[len(pvs):]:
                    row(path, "missing", None, gv)
    return rows


def prf(tp: float, fp: float, fn: float) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f
