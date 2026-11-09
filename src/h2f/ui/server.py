"""Server page: how the middleware is behaving, from its own request log.

Depends on `h2f.obs` (LogStore + analytics + probe) and `charts`. It knows nothing about the sim UI or the database.
Three lenses, in the order an on-call engineer asks the questions:
    Pulse        "is it healthy right now?"           RED metrics, trend, latency shape, failure causes
    Requests     "what happened to *that* request?"   filterable log -> click a row -> waterfall of its HAPI calls
    Dependencies "where does the time go?"            own time vs HAPI, per-route x per-call heatmap, live probes
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from h2f.obs import analytics as an
from h2f.obs.probe import probe
from h2f.obs.store import LogStore
from h2f.ui import charts

DISPLAY_TZ = "Asia/Kolkata"
WINDOWS = {"Last 15 min": "15min", "Last hour": "1h", "Last 6 hours": "6h", "Last 24 hours": "24h", "Everything loaded": None}


def _delta(cur: float, prev: float, unit: str = "", invert: bool = True) -> str | None:
    if not prev:
        return None
    d = cur - prev
    return f"{d:+.1f}{unit}"


def server_page(store: LogStore, *, middleware_url: str, hapi_url: str) -> None:
    st.title("Server")
    st.caption("The NICU middleware as it sees itself: one structured log line per request (no bodies, no patient ids), with every HAPI call it made.")

    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    win = c1.segmented_control("Window", list(WINDOWS), default="Last hour", label_visibility="collapsed") or "Last hour"
    limit = c2.selectbox("Load last", [1000, 5000, 20000], index=1, format_func=lambda n: f"{n:,} records")
    live = c3.toggle("Live (5 s)", value=False)

    @st.fragment(run_every=5 if live else None)
    def body() -> None:
        logs = store.read(limit)
        if not logs:
            st.info("No requests logged yet. Start the middleware (`make sim-api`) and use the Client page; each call appears here.")
            return
        df = an.to_frame(logs)
        now = pd.Timestamp.now(tz="UTC")
        span = WINDOWS[win]
        if span:
            df = df[df["ts"] >= now - pd.Timedelta(span)]
        ids = {l.request_id for l in logs} & set(df["request_id"])
        logs = [l for l in logs if l.request_id in ids]
        if df.empty:
            st.warning(f"No requests in “{win}”. Widen the window.")
            return
        t_pulse, t_req, t_dep = st.tabs([":material/monitor_heart: Pulse", ":material/manage_search: Requests", ":material/account_tree: Dependencies"])
        with t_pulse:
            _pulse(df, span)
        with t_req:
            _requests(df, logs)
        with t_dep:
            _dependencies(df, logs, middleware_url, hapi_url)

    body()


def _local(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(ts=df["ts"].dt.tz_convert(DISPLAY_TZ).dt.tz_localize(None))


# ----------------------------------------------------------------------------------------------- lens 1
def _pulse(df: pd.DataFrame, span: str | None) -> None:
    now = df["ts"].max()
    if span:
        cur, prev = an.compare_windows(df, pd.Timedelta(span), now)
        prev_ok = prev["requests"] > 0
    else:
        cur, prev, prev_ok = an.kpis(df), {}, False
    cols = st.columns(6)
    cols[0].metric("Requests", f"{cur['requests']:,}", f"{cur['requests'] - prev['requests']:+d}" if prev_ok else None, border=True)
    cols[1].metric("Throughput", f"{cur['rpm']:.1f}/min", border=True)
    cols[2].metric("Server errors", f"{cur['error_rate']:.1f}%", _delta(cur["error_rate"], prev.get("error_rate", 0), " pts") if prev_ok else None, delta_color="inverse", border=True,
                   help="5xx and 502/503/504 only: faults that are ours or HAPI's. A 4xx is the caller's problem and is counted separately.")
    cols[3].metric("Rejected (4xx)", f"{cur['client_error_rate']:.1f}%", border=True, help="Validation 422, conflict 409, auth 401/403, other 4xx. Expected in normal use; a jump means a client bug or an attack.")
    cols[4].metric("p50 · p95", f"{cur['p50']:.0f} · {cur['p95']:.0f} ms", _delta(cur["p95"], prev.get("p95", 0), " ms") if prev_ok else None, delta_color="inverse", border=True,
                   help="Median and 95th percentile. Averages hide the slow tail users actually feel.")
    cols[5].metric("Time in HAPI", f"{cur['upstream_share']:.0f}%", border=True, help="Share of total request time spent waiting on HAPI. High = look at the dependency, low = look at our own code.")

    freq = "10s" if span == "15min" else "1min" if span in ("1h", "6h") else "10min"
    ts = _local(df.rename(columns={})).pipe(lambda d: an.timeseries(d, freq))
    st.markdown("##### Traffic and latency over time")
    st.altair_chart(charts.pulse(ts), width="stretch")
    st.space("large")
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown("##### Why requests fail")
        st.altair_chart(charts.category_bars(an.by_category(df)), width="stretch")
        st.caption("Triage by cause, not status code: **conflict** = someone edited first, **validation** = bad form, **upstream_error** = HAPI is down or slow.")
    with b:
        st.markdown("##### Latency distribution")
        st.altair_chart(charts.latency_hist(an.histogram(df), cur["p95"]), width="stretch")
        st.caption("Log scale. A second hump to the right means two populations, usually a cache hit vs miss or a slow route.")


# ----------------------------------------------------------------------------------------------- lens 2
def _requests(df: pd.DataFrame, logs: list) -> None:
    with st.container(border=True):
        f1, f2, f3, f4 = st.columns(4)
        routes = f1.multiselect("Route", sorted(df["route"].unique()))
        cats = f2.multiselect("Cause", [c for c in an.CATEGORIES if c in set(df["category"])])
        roles = f3.multiselect("Role", sorted(x for x in df["role"].unique() if x))
        ops = f4.multiselect("Operation", sorted(x for x in df["op"].unique() if x))
        g1, g2 = st.columns([1, 2])
        min_ms = g1.slider("Slower than (ms)", 0, int(max(df["ms"].max(), 10)), 0)
        text = g2.text_input("Request id or practitioner", placeholder="paste a request_id from the Client trace…")
    f = an.filter_frame(df, routes=routes, categories=cats, roles=roles, ops=ops, min_ms=min_ms, text=text)
    st.caption(f"{len(f):,} of {len(df):,} requests · newest first · click a row to open its waterfall")
    show = _local(f).sort_values("ts", ascending=False)[["ts", "method", "route", "status", "category", "ms", "upstream_n", "self_ms", "op", "outcome", "role", "request_id"]]
    ev = st.dataframe(show, hide_index=True, width="stretch", height=300, on_select="rerun", selection_mode="single-row", key="req_table",
                      column_config={"ts": st.column_config.DatetimeColumn("time (IST)", format="HH:mm:ss.SSS"),
                                     "ms": st.column_config.ProgressColumn("ms", min_value=0, max_value=float(max(df["ms"].max(), 1)), format="%.0f"),
                                     "upstream_n": st.column_config.NumberColumn("HAPI calls"), "self_ms": st.column_config.NumberColumn("own ms")})
    if not ev.selection.rows:
        st.info("Select a request to see where its time went.")
        return
    rid = show.iloc[ev.selection.rows[0]]["request_id"]
    rec = next(l for l in logs if l.request_id == rid)
    l, r = st.columns([3, 2])
    with l:
        st.markdown(f"##### Waterfall · `{rec.method} {rec.route}` · {rec.ms:.0f} ms · HTTP {rec.status}")
        st.altair_chart(charts.waterfall(an.waterfall(rec)), width="stretch")
        st.caption("Top bar = the whole request. Blue bars = calls to HAPI (red = HAPI answered ≥ 400). Whitespace = the middleware's own work.")
    with r:
        st.markdown("##### Raw record")
        st.json(rec.to_dict(), expanded=1)
        st.caption(f"Database link: in the **Database** page, a row written by this request carries `request_id = {rid}`.")


# ----------------------------------------------------------------------------------------------- lens 3
def _dependencies(df: pd.DataFrame, logs: list, middleware_url: str, hapi_url: str) -> None:
    up = an.upstream_frame(logs)
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown("##### Where the time goes")
        st.altair_chart(charts.self_vs_upstream(_local(df)), width="stretch")
        st.caption("Purple = the middleware's own compute (parsing, rules, mapping). Blue = waiting on HAPI. Network cost to HAPI is inside blue.")
    with b:
        st.markdown("##### Route latency")
        st.altair_chart(charts.route_latency(an.by_route(df)), width="stretch")
    st.space("large")
    st.markdown("##### Which route calls which HAPI interaction (total ms)")
    st.altair_chart(charts.route_heatmap(up), width="stretch")
    st.space("large")
    st.markdown("##### HAPI calls ranked by total time")
    st.dataframe(an.upstream_summary(up), hide_index=True, width="stretch")
    st.caption("Sort order is **total** time, not p95: a fast call made 50× per request costs more than one slow call. Fix the top row first.")

    st.space("large")
    st.markdown("##### Live probes")
    st.caption("Passive logs say how real traffic went; probes say whether the path works *now* (and are the only signal when there is no traffic). "
               "Cold = new connection; warm = reused. The gap is connection setup, i.e. network cost.")
    if st.button("Probe now", icon=":material/network_ping:"):
        cols = st.columns(2)
        for col, (name, url) in zip(cols, [("Middleware", f"{middleware_url}/health"), ("HAPI", f"{hapi_url}/metadata")], strict=True):
            r = probe(name, url)
            with col, st.container(border=True):
                st.markdown(f"**{name}** · `{url}`")
                if not r.ok:
                    st.error(f"unreachable ({r.error or r.status})")
                    continue
                m1, m2, m3 = st.columns(3)
                m1.metric("cold", f"{r.cold_ms:.0f} ms")
                m2.metric("warm p50", f"{r.warm_p50:.0f} ms")
                m3.metric("connect cost", f"{r.network_ms:.0f} ms")
