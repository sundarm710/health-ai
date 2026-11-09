"""Chart builders: DataFrame in, Altair (= Vega-Lite spec) out. No Streamlit, no data access.

Palette: categorical slots in fixed order from the validated reference palette; status red/amber reserved for
failure/slow. Axes and grids are recessive; ink colours are left to the host theme so light/dark both work.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"
GREY = "#898781"
CRITICAL, SERIOUS, WARNING = "#d03b3b", "#ec835a", "#fab219"

CATEGORY_COLORS = {"ok": BLUE, "validation": YELLOW, "conflict": ORANGE, "auth": VIOLET, "client_error": GREY,
                   "upstream_error": SERIOUS, "server_error": CRITICAL}
KIND_COLORS = {"created": BLUE, "updated": AQUA, "deleted": CRITICAL}


def _cfg(c: alt.Chart | alt.LayerChart | alt.VConcatChart) -> alt.Chart:
    return (c.properties(padding={"left": 8, "right": 56, "top": 10, "bottom": 10}).configure_view(stroke=None)
            .configure_axis(gridColor="#88878126", domainColor="#88878155", tickColor="#88878155", labelColor=GREY, titleColor=GREY,
                            labelFontSize=12, titleFontSize=12, labelPadding=10, titlePadding=16, tickCount=5, labelLimit=260)
            .configure_axisX(grid=False, labelOverlap="greedy")  # vertical gridlines are noise on time / log axes
            .configure_axisY(labelOverlap=False)  # never silently drop a category label
            .configure_legend(labelColor=GREY, titleColor=GREY, orient="top", symbolType="circle", symbolSize=110, labelFontSize=12,
                              padding=6, offset=14, columnPadding=22, labelOffset=8)
            .configure_title(anchor="start", fontSize=12, color=GREY, offset=10, fontWeight="normal"))


def _step(row: int = 36) -> alt.Step:
    """Fixed pixels per category row: height follows the data, so labels and bars never get squeezed together."""
    return alt.Step(row)


def _empty(msg: str = "no data yet") -> alt.Chart:
    return alt.Chart(pd.DataFrame({"t": [msg]})).mark_text(color=GREY, fontSize=14).encode(text="t:N").properties(height=80)


# ----------------------------------------------------------------------------------------------- server
def pulse(ts: pd.DataFrame, height: int = 130) -> alt.Chart:
    """Rate (bars, 5xx overlaid) above latency (p50/p95 lines, direct-labelled): two measures, two aligned panels, never a dual axis."""
    if ts.empty:
        return _empty()
    tip = [alt.Tooltip("ts:T", title="time", format="%H:%M:%S"), alt.Tooltip("requests:Q"), alt.Tooltip("errors:Q", title="5xx"),
           alt.Tooltip("p50:Q", format=".0f", title="p50 ms"), alt.Tooltip("p95:Q", format=".0f", title="p95 ms")]
    top_x = alt.X("ts:T", title=None, axis=alt.Axis(labels=False, ticks=False))
    base = alt.Chart(ts).encode(x=top_x, tooltip=tip)
    rate = (base.mark_bar(color=BLUE, cornerRadiusTopLeft=3, cornerRadiusTopRight=3, size=10).encode(y=alt.Y("requests:Q", title="requests"))
            + base.mark_bar(color=CRITICAL, cornerRadiusTopLeft=3, cornerRadiusTopRight=3, size=10).encode(y="errors:Q")
            ).properties(height=height, title="Requests per interval  (red = server errors)")
    lat = ts.melt("ts", ["p50", "p95"], var_name="q", value_name="ms")
    color = alt.Color("q:N", scale=alt.Scale(domain=["p50", "p95"], range=[BLUE, ORANGE]), legend=None)
    x = alt.X("ts:T", title=None, axis=alt.Axis(format="%H:%M", tickCount=8, labelFlush=False))
    line = alt.Chart(lat).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=45, filled=True)).encode(
        x=x, y=alt.Y("ms:Q", title="latency (ms)"), color=color, tooltip=[alt.Tooltip("ts:T", format="%H:%M:%S"), "q:N", alt.Tooltip("ms:Q", format=".0f")])
    last = lat.sort_values("ts").groupby("q").tail(1)
    # direct end labels, nudged apart (p95 above its point, p50 below) so they never overprint each other
    def label(q: str, dy: int):
        return alt.Chart(last[last["q"] == q]).mark_text(align="left", dx=10, dy=dy, fontSize=12, fontWeight="bold").encode(
            x="ts:T", y="ms:Q", text="q:N", color=color)

    names = label("p95", -10) + label("p50", 10)
    lines = (line + names).properties(height=height, title="Latency  (p50 = typical, p95 = slow tail)")
    return _cfg(alt.vconcat(rate, lines, spacing=36).resolve_scale(x="shared"))


def latency_hist(h: pd.DataFrame, p95: float | None = None) -> alt.Chart:
    if h.empty:
        return _empty()
    h = h.assign(label=h.apply(lambda r: f"{r.lo:g}–{r.hi:g} ms", axis=1))
    bars = alt.Chart(h).mark_bar(color=BLUE, cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        x=alt.X("lo:Q", scale=alt.Scale(type="log"), title="latency (ms, log scale)", axis=alt.Axis(grid=False, tickCount=4, format="~s")), x2="hi:Q",
        y=alt.Y("requests:Q", title="requests", scale=alt.Scale(zero=True, nice=True)), y2=alt.datum(0), tooltip=["label:N", "requests:Q"])
    if p95:
        rule = alt.Chart(pd.DataFrame({"x": [p95], "t": [f"p95 {p95:g} ms"]})).mark_rule(color=ORANGE, strokeWidth=2, strokeDash=[4, 3]).encode(x="x:Q", tooltip="t:N")
        bars = bars + rule
    return _cfg(bars.properties(height=200))


def category_bars(c: pd.DataFrame) -> alt.Chart:
    c = c[c["requests"] > 0]
    if c.empty:
        return _empty()
    order = list(CATEGORY_COLORS)
    base = alt.Chart(c).encode(y=alt.Y("category:N", sort=order, title=None, scale=alt.Scale(paddingInner=0.45)))
    bars = base.mark_bar(cornerRadiusTopRight=3, cornerRadiusBottomRight=3).encode(
        x=alt.X("requests:Q", title=None, axis=alt.Axis(tickCount=4, grid=True, format="d")),
        color=alt.Color("category:N", scale=alt.Scale(domain=order, range=list(CATEGORY_COLORS.values())), legend=None), tooltip=["category:N", "requests:Q"])
    n = base.mark_text(align="left", dx=6, fontSize=12, color=GREY).encode(x="requests:Q", text="requests:Q")
    return _cfg((bars + n).properties(height=_step(40)))


def waterfall(wf: pd.DataFrame) -> alt.Chart:
    """Gantt of one request: the bar on top is the whole request; indented bars are calls to HAPI; the gaps are our own time."""
    tone = wf.apply(lambda r: "request" if r["kind"] == "request" else "failed" if r["status"] >= 400 else "ok", axis=1)
    wf = wf.assign(ms=(wf["end"] - wf["start"]).round(1), tone=tone)
    order = list(wf["label"])
    color = alt.Color("tone:N", scale=alt.Scale(domain=["request", "ok", "failed"], range=[GREY, BLUE, CRITICAL]), legend=None)
    bars = alt.Chart(wf).mark_bar(cornerRadius=3, size=16).encode(
        y=alt.Y("label:N", sort=order, title=None, scale=alt.Scale(paddingInner=0.4)), x=alt.X("start:Q", title="ms since request start", axis=alt.Axis(grid=True, tickCount=6), scale=alt.Scale(domainMin=0, nice=True)), x2="end:Q", color=color,
        tooltip=["label:N", alt.Tooltip("start:Q", format=".1f"), alt.Tooltip("ms:Q", title="duration ms"), "status:Q"])
    txt = alt.Chart(wf).mark_text(align="left", dx=4, color=GREY, fontSize=11).encode(y=alt.Y("label:N", sort=order), x="end:Q", text=alt.Text("ms:Q", format=".0f"))
    return _cfg((bars + txt).properties(height=_step(34)))


def route_latency(r: pd.DataFrame) -> alt.Chart:
    if r.empty:
        return _empty()
    r = r.assign(name=r["method"] + " " + r["route"])
    long = r.melt("name", ["p50", "p95"], var_name="q", value_name="ms")
    return _cfg(alt.Chart(long).mark_bar(cornerRadiusTopRight=3, cornerRadiusBottomRight=3).encode(
        y=alt.Y("name:N", title=None, sort=list(r.sort_values("p95", ascending=False)["name"]), scale=alt.Scale(paddingInner=0.35)),
        x=alt.X("ms:Q", title="ms", axis=alt.Axis(grid=True)), yOffset=alt.YOffset("q:N", scale=alt.Scale(paddingInner=0.15)), color=alt.Color("q:N", scale=alt.Scale(domain=["p50", "p95"], range=[BLUE, ORANGE]), title=None),
        tooltip=["name:N", "q:N", alt.Tooltip("ms:Q", format=".1f")]).properties(height=_step(48)))


def self_vs_upstream(df: pd.DataFrame, freq: str = "1min") -> alt.Chart:
    """Where did the time go: our own code vs waiting on HAPI. Stacked mean ms per bucket."""
    if df.empty:
        return _empty()
    g = df.set_index("ts").groupby(pd.Grouper(freq=freq))[["self_ms", "upstream_ms"]].mean().dropna().reset_index()
    long = g.melt("ts", var_name="part", value_name="ms").replace({"self_ms": "middleware (own time)", "upstream_ms": "HAPI (waiting)"})
    return _cfg(alt.Chart(long).mark_area(opacity=0.9, line=True).encode(
        x=alt.X("ts:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("ms:Q", title="mean ms per request", stack=True),
        color=alt.Color("part:N", scale=alt.Scale(range=[VIOLET, BLUE]), title=None), tooltip=["ts:T", "part:N", alt.Tooltip("ms:Q", format=".1f")]).properties(height=200))


def route_heatmap(up: pd.DataFrame) -> alt.Chart:
    """Which route spends its time on which HAPI call: cell = total ms."""
    if up.empty:
        return _empty()
    g = up.assign(call=up["interaction"] + " " + up["resource"]).groupby(["route", "call"], as_index=False)["ms"].sum()
    return _cfg(alt.Chart(g).mark_rect(cornerRadius=3, stroke="#00000000", strokeWidth=3).encode(
        x=alt.X("call:N", title=None, axis=alt.Axis(labelAngle=-35, labelLimit=200, labelOverlap=False)), y=alt.Y("route:N", title=None),
        color=alt.Color("ms:Q", scale=alt.Scale(range=["#cde2fb", "#0d366b"]), title="total ms", legend=alt.Legend(gradientLength=220, format="~s", tickCount=4)), tooltip=["route:N", "call:N", alt.Tooltip("ms:Q", format=".0f")]).properties(height=_step(44)))


# ----------------------------------------------------------------------------------------------- database
def timeline(tl: pd.DataFrame) -> alt.Chart:
    """Swim-lane: one row per resource type, one dot per stored version, colour = created / updated / deleted."""
    if tl.empty:
        return _empty("no events")
    order = list(tl.groupby("res_type")["when"].min().sort_values().index)
    sel = alt.selection_point(fields=["res_id", "res_ver"], name="pick")
    return _cfg(alt.Chart(tl).mark_point(filled=True, size=130, stroke="white", strokeWidth=1.5, opacity=0.95).encode(
        x=alt.X("when:T", title=None, axis=alt.Axis(format="%d %b %H:%M:%S", tickCount=5, labelOverlap="greedy")), y=alt.Y("res_type:N", sort=order, title=None, scale=alt.Scale(paddingInner=0.3)),
        color=alt.Color("kind:N", scale=alt.Scale(domain=list(KIND_COLORS), range=list(KIND_COLORS.values())), title=None),
        tooltip=[alt.Tooltip("when:T", format="%Y-%m-%d %H:%M:%S"), "res_type:N", "fhir_id:N", "res_ver:Q", "kind:N", "summary:N", "request_id:N"]
    ).add_params(sel).properties(height=_step(44)))


def activity(a: pd.DataFrame, top: int = 7) -> alt.Chart:
    if a.empty:
        return _empty()
    keep = a.groupby("res_type")["writes"].sum().nlargest(top).index
    a = a.assign(res_type=a["res_type"].where(a["res_type"].isin(keep), "Other")).groupby(["ts", "res_type"], as_index=False)["writes"].sum()
    domain = [*keep, "Other"] if (a["res_type"] == "Other").any() else list(keep)
    pal = [BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET][: len(keep)] + ([GREY] if "Other" in domain else [])
    return _cfg(alt.Chart(a).mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        x=alt.X("ts:T", title=None), y=alt.Y("writes:Q", title="versions written"),
        color=alt.Color("res_type:N", scale=alt.Scale(domain=domain, range=pal), title=None), tooltip=["ts:T", "res_type:N", "writes:Q"]).properties(height=200))


def resource_counts(c: pd.DataFrame) -> alt.Chart:
    if c.empty:
        return _empty()
    long = c.melt("res_type", ["resources", "versions"], var_name="what", value_name="n")
    return _cfg(alt.Chart(long).mark_bar(cornerRadiusTopRight=3, cornerRadiusBottomRight=3).encode(
        y=alt.Y("res_type:N", sort=list(c["res_type"]), title=None, scale=alt.Scale(paddingInner=0.35)), x=alt.X("n:Q", title=None, axis=alt.Axis(grid=True)),
        yOffset=alt.YOffset("what:N", scale=alt.Scale(paddingInner=0.15)),
        color=alt.Color("what:N", scale=alt.Scale(domain=["resources", "versions"], range=[BLUE, AQUA]), title=None),
        tooltip=["res_type:N", "what:N", "n:Q"]).properties(height=_step(52)))


def link_heatmap(m: pd.DataFrame) -> alt.Chart:
    """Who references whom (row = referrer, column = referenced). The schema of the data, as it actually is."""
    if m.empty:
        return _empty()
    return _cfg(alt.Chart(m).mark_rect(cornerRadius=3, stroke="#00000000", strokeWidth=3).encode(
        x=alt.X("target:N", title="referenced →", axis=alt.Axis(labelAngle=-35, labelOverlap=False)), y=alt.Y("source:N", title="referrer ↓"),
        color=alt.Color("links:Q", scale=alt.Scale(type="sqrt", range=["#cde2fb", "#0d366b"]), title="links", legend=alt.Legend(gradientLength=220, tickCount=4)),
        tooltip=["source:N", "target:N", "links:Q"]).properties(height=_step(34)))
