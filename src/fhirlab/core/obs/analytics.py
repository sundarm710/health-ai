"""Pure functions: list[RequestLog] -> DataFrames/dicts. No I/O, no UI.

The shapes returned here are the "view models" any front end renders. Percentiles, not averages:
a mean hides the slow tail that users actually feel.
"""

from __future__ import annotations

import pandas as pd

from fhirlab.core.obs.record import CATEGORIES, RequestLog

COLS = ["ts", "request_id", "method", "route", "status", "ms", "category", "bytes_out", "actor", "role", "op", "outcome",
        "error", "upstream_n", "upstream_ms", "self_ms"]


def to_frame(logs: list[RequestLog]) -> pd.DataFrame:
    rows = []
    for r in logs:
        up = sum(u.ms for u in r.upstream)
        rows.append({**{k: v for k, v in r.to_dict().items() if k != "upstream"},
                     "upstream_n": len(r.upstream), "upstream_ms": round(up, 1), "self_ms": round(max(r.ms - up, 0), 1)})
    df = pd.DataFrame(rows, columns=COLS)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df.sort_values("ts").reset_index(drop=True)


def filter_frame(df: pd.DataFrame, *, since: pd.Timestamp | None = None, routes=None, categories=None, roles=None,
                 ops=None, min_ms: float = 0, text: str = "") -> pd.DataFrame:
    m = pd.Series(True, index=df.index)
    if since is not None:
        m &= df["ts"] >= since
    for col, vals in (("route", routes), ("category", categories), ("role", roles), ("op", ops)):
        if vals:
            m &= df[col].isin(vals)
    if min_ms:
        m &= df["ms"] >= min_ms
    if text:
        m &= df["request_id"].str.contains(text.strip(), case=False) | df["actor"].str.contains(text.strip(), case=False)
    return df[m]


def _q(s: pd.Series, q: float) -> float:
    return round(float(s.quantile(q)), 1) if len(s) else 0.0


def kpis(df: pd.DataFrame) -> dict:
    """RED: Rate, Errors, Duration. 'errors' = server-side faults only; 4xx are the caller's problem, tracked apart."""
    n = len(df)
    span_s = max((df["ts"].max() - df["ts"].min()).total_seconds(), 1) if n > 1 else 1
    return {
        "requests": n,
        "rpm": round(n / span_s * 60, 2) if n > 1 else 0.0,
        "error_rate": round(float(df["category"].isin(["server_error", "upstream_error"]).mean() * 100), 2) if n else 0.0,
        "client_error_rate": round(float(df["category"].isin(["validation", "conflict", "auth", "client_error"]).mean() * 100), 2) if n else 0.0,
        "p50": _q(df["ms"], 0.5), "p95": _q(df["ms"], 0.95), "p99": _q(df["ms"], 0.99),
        "upstream_share": round(float(df["upstream_ms"].sum() / df["ms"].sum() * 100), 1) if n and df["ms"].sum() else 0.0,
    }


def compare_windows(df: pd.DataFrame, window: pd.Timedelta, now: pd.Timestamp) -> tuple[dict, dict]:
    """KPIs for the last `window` and for the window before it (to show deltas)."""
    cur = df[df["ts"] >= now - window]
    prev = df[(df["ts"] < now - window) & (df["ts"] >= now - 2 * window)]
    return kpis(cur), kpis(prev)


def timeseries(df: pd.DataFrame, freq: str = "1min") -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["ts", "requests", "errors", "p50", "p95"])
    g = df.set_index("ts").groupby(pd.Grouper(freq=freq))
    out = pd.DataFrame({
        "requests": g.size(),
        "errors": g["category"].apply(lambda s: int(s.isin(["server_error", "upstream_error"]).sum())),
        "p50": g["ms"].quantile(0.5), "p95": g["ms"].quantile(0.95),
    }).reset_index()
    return out


def by_category(df: pd.DataFrame) -> pd.DataFrame:
    c = df["category"].value_counts().reindex(CATEGORIES, fill_value=0)
    return c.rename_axis("category").reset_index(name="requests")


def by_route(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["route", "method", "requests", "p50", "p95", "max", "fail_pct", "upstream_pct"])
    g = df.groupby(["method", "route"])
    return g.apply(lambda d: pd.Series({
        "requests": len(d), "p50": _q(d["ms"], .5), "p95": _q(d["ms"], .95), "max": d["ms"].max(),
        "fail_pct": round(float((d["category"] != "ok").mean() * 100), 1),
        "upstream_pct": round(float(d["upstream_ms"].sum() / d["ms"].sum() * 100), 1) if d["ms"].sum() else 0.0,
    }), include_groups=False).reset_index().sort_values("requests", ascending=False)


def histogram(df: pd.DataFrame, bins: int = 30) -> pd.DataFrame:
    """Log-spaced latency buckets (latency is heavy-tailed; linear bins hide everything left of the tail)."""
    if df.empty:
        return pd.DataFrame(columns=["lo", "hi", "requests"])
    import numpy as np

    lo, hi = max(df["ms"].min(), 0.1), max(df["ms"].max(), 0.2)
    edges = np.geomspace(lo, hi * 1.001, bins + 1)
    cnt, _ = np.histogram(df["ms"].clip(lower=lo), bins=edges)
    return pd.DataFrame({"lo": edges[:-1].round(1), "hi": edges[1:].round(1), "requests": cnt})


def upstream_frame(logs: list[RequestLog]) -> pd.DataFrame:
    rows = [{"ts": pd.Timestamp(r.ts), "request_id": r.request_id, "route": r.route, "op": r.op, **u.__dict__}
            for r in logs for u in r.upstream]
    cols = ["ts", "request_id", "route", "op", "interaction", "resource", "method", "status", "ms", "start_ms"]
    return pd.DataFrame(rows, columns=cols)


def upstream_summary(up: pd.DataFrame) -> pd.DataFrame:
    if up.empty:
        return pd.DataFrame(columns=["interaction", "resource", "calls", "p50", "p95", "total_ms", "errors"])
    g = up.groupby(["interaction", "resource"])
    return g.apply(lambda d: pd.Series({
        "calls": len(d), "p50": _q(d["ms"], .5), "p95": _q(d["ms"], .95), "total_ms": round(d["ms"].sum(), 1),
        "errors": int((d["status"] >= 400).sum()),
    }), include_groups=False).reset_index().sort_values("total_ms", ascending=False)


def waterfall(log: RequestLog) -> pd.DataFrame:
    """Gantt rows for one request: the request itself, each dependency call, and the gaps (own compute)."""
    rows = [{"label": f"{log.method} {log.route}", "kind": "request", "start": 0.0, "end": log.ms, "status": log.status}]
    for i, u in enumerate(log.upstream, 1):
        rows.append({"label": f"{i}. {u.interaction} {u.resource}", "kind": "upstream", "start": u.start_ms,
                     "end": u.start_ms + u.ms, "status": u.status})
    return pd.DataFrame(rows)


def route_distribution(df: pd.DataFrame, top: int = 10, bins: int = 28) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Latency distribution per route for the `top` busiest routes.

    Returns (buckets, stats). Buckets share ONE set of log-spaced edges across routes so rows line up on a common axis:
    columns route, lo, hi, requests, share (fraction of that route's requests). Stats: route, requests, p50, p95, max.
    Routes are keyed "METHOD /path" and ordered busiest first.
    """
    import numpy as np

    cols_b, cols_s = ["route", "lo", "hi", "requests", "share"], ["route", "requests", "p50", "p95", "max"]
    if df.empty:
        return pd.DataFrame(columns=cols_b), pd.DataFrame(columns=cols_s)
    key = df["method"] + " " + df["route"]
    keep = key.value_counts().head(top).index
    d = df.assign(route_key=key)[key.isin(keep)]
    edges = np.geomspace(max(d["ms"].min(), 0.1), max(d["ms"].max(), 0.2) * 1.001, bins + 1)
    brows, srows = [], []
    for r in keep:
        ms = d.loc[d["route_key"] == r, "ms"]
        cnt, _ = np.histogram(ms.clip(lower=edges[0]), bins=edges)
        brows += [{"route": r, "lo": round(float(lo), 2), "hi": round(float(hi), 2), "requests": int(c), "share": c / len(ms)}
                  for lo, hi, c in zip(edges[:-1], edges[1:], cnt, strict=True)]
        srows.append({"route": r, "requests": len(ms), "p50": _q(ms, .5), "p95": _q(ms, .95), "max": round(float(ms.max()), 1)})
    return pd.DataFrame(brows, columns=cols_b), pd.DataFrame(srows, columns=cols_s)
