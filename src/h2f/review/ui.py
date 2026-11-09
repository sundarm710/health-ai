"""Pages for the expert review app (entry point: app/review_app.py)."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from h2f.eval.charts import COLOR, CONFIG_ORDER, summarize
from h2f.eval.score import prf
from h2f.review import store
from h2f.review.data import load_detail, load_results, render_v2

VERDICT_LABELS = list(store.VERDICTS.values())
LABEL_TO_VERDICT = {v: k for k, v in store.VERDICTS.items()}
ESC_LABELS = list(store.ESCALATION.values())
LABEL_TO_ESC = {v: k for k, v in store.ESCALATION.items()}


@st.cache_data
def results() -> pd.DataFrame:
    return load_results()


@st.cache_data(max_entries=512)
def detail(config: str, level: float, msg_id: str):
    return load_detail(config, level, msg_id)


def color_scale(configs: list[str]) -> alt.Scale:
    return alt.Scale(domain=configs, range=[COLOR[c] for c in configs])


# --- shared sidebar filters --------------------------------------------------------------------

def sidebar(df: pd.DataFrame) -> pd.DataFrame:
    with st.sidebar:
        st.text_input("Reviewer", key="reviewer", placeholder="your name")
        st.divider()
        configs = [c for c in CONFIG_ORDER if c in set(df.config)]
        sel_cfg = st.multiselect("Config", configs, default=configs)
        splits = st.multiselect("Split", sorted(df.split.unique()), default=sorted(df.split.unique()))
        types = st.multiselect("Message type", sorted(df.msg_type.unique()), default=sorted(df.msg_type.unique()))
        all_c = sorted({c for cs in df.corruptions for c in cs})
        ctypes = st.multiselect("Has corruption type", all_c, help="Empty = no filter")
    out = df[df.config.isin(sel_cfg) & df.split.isin(splits) & df.msg_type.isin(types)]
    if ctypes:
        out = out[out.corruptions.apply(lambda cs: bool(set(cs) & set(ctypes)))]
    return out


# --- page: trends ------------------------------------------------------------------------------

def page_trends() -> None:
    df = st.session_state.filtered
    if df.empty:
        st.info("No messages match the filters.")
        return
    st.title("Trends")
    configs = [c for c in CONFIG_ORDER if c in set(df.config)]
    summ = df.groupby(["config", "level"]).apply(summarize, include_groups=False).reset_index()

    level = st.select_slider("Corruption level", options=sorted(df.level.unique()), value=0.4)
    for cfg in configs:
        now = summ[(summ.config == cfg) & (summ.level == level)].iloc[0]
        base = summ[(summ.config == cfg) & (summ.level == 0.0)].iloc[0]
        silent = df[(df.config == cfg) & (df.level == level) & (df.path == "fast") & (df.wrong_crit + df.fn_crit > 0)]
        st.caption(f"**{cfg}** · {int(now.n)} messages at level {level}")
        k = st.columns(5)
        k[0].metric("Critical F1", f"{now.crit_F1:.3f}", f"{now.crit_F1 - base.crit_F1:+.3f} vs clean")
        k[1].metric("Escalated", f"{now.escalated_pct:.0f}%", f"{now.escalated_pct - base.escalated_pct:+.0f} pts", delta_color="inverse")
        k[2].metric("Valid bundles", f"{now.valid_pct:.0f}%")
        k[3].metric("Wrong critical values / msg", f"{now.wrong_per_msg:.2f}", delta_color="off")
        k[4].metric("Silent failures", len(silent), help="Auto-posted (not escalated) yet has wrong or missing critical values. Nobody would ever look at these.")

    st.subheader("By corruption level")
    cols = st.columns(3)
    for col, (field, title, fmt) in zip(
        cols,
        [("crit_F1", "Critical-field F1", ".3f"), ("escalated_pct", "Escalated to human (%)", ".0f"),
         ("wrong_per_msg", "Wrong critical values per message", ".2f")],
    ):
        base = alt.Chart(summ).encode(
            x=alt.X("level:Q", title="corruption level", axis=alt.Axis(values=sorted(summ.level.unique()))),
            y=alt.Y(f"{field}:Q", title=None, scale=alt.Scale(zero=field != "crit_F1")),
            color=alt.Color("config:N", scale=color_scale(configs), legend=alt.Legend(orient="bottom") if len(configs) > 1 else None),
            tooltip=["config", "level", alt.Tooltip(f"{field}:Q", format=fmt), alt.Tooltip("n:Q", title="messages")],
        )
        chart = base.mark_line(strokeWidth=2) + base.mark_point(size=60, filled=True)
        col.markdown(f"**{title}**")
        col.altair_chart(chart.properties(height=240), width="stretch")

    st.subheader("Does the rules layer detect each corruption?")
    st.caption(
        "Messages with exactly one corruption type (levels > 0). Each cell: share of those messages where the "
        "rules layer raised that failure kind. **No flag** = the corruption passed silently."
    )
    cfg = st.selectbox("Config", configs, key="det_cfg") if len(configs) > 1 else configs[0]
    x = df[(df.config == cfg) & (df.level > 0)].copy()
    x["ctype"] = x.corruptions.apply(lambda cs: [c for c in cs if c != "PROMPT_INJECTION_NTE"])
    x = x[x.ctype.apply(len) == 1]
    x["ctype"] = x.ctype.str[0]
    x["kind"] = x.rule_errors.apply(lambda r: r or ["(no flag)"])
    if x.empty:
        st.info("No single-corruption messages under current filters.")
    else:
        m = x.explode("kind").groupby(["ctype", "kind"]).size().rename("n").reset_index()
        m = m.merge(x.groupby("ctype").size().rename("total").reset_index(), on="ctype")
        m["share"] = m.n / m.total
        heat = alt.Chart(m).encode(
            x=alt.X("kind:N", title="failure kind raised by rules", sort=sorted(m["kind"].unique())),
            y=alt.Y("ctype:N", title="injected corruption"),
            tooltip=["ctype", "kind", alt.Tooltip("share:Q", format=".0%"), "n", "total"],
        )
        chart = heat.mark_rect(cornerRadius=3).encode(
            color=alt.Color("share:Q", scale=alt.Scale(range=["#cde2fb", "#104281"], domain=[0, 1]), legend=None)
        ) + heat.mark_text(fontSize=11).encode(
            text=alt.Text("share:Q", format=".0%"),
            color=alt.when(alt.datum.share > 0.5).then(alt.value("white")).otherwise(alt.value("#1f1f1e")),
        )
        st.altair_chart(chart.properties(height=320), width="stretch")

    st.subheader("Expert calibration")
    fa, ma = store.fields_df(), store.messages_df()
    if fa.empty and ma.empty:
        st.info("No annotations yet. Review messages in the **Review queue** to calibrate the automatic metric.")
        return
    reviewed = fa[["config", "level", "msg_id"]].drop_duplicates()
    auto = {"tp": 0, "fp": 0, "fn": 0}
    adj = dict(auto)
    for r in reviewed.itertuples():
        d = detail(r.config, r.level, r.msg_id)
        for row in d.diff:
            if not row["critical"]:
                continue
            auto["tp"] += row["status"] == "match"
            auto["fp"] += row["status"] in ("wrong", "extra")
            auto["fn"] += row["status"] in ("wrong", "missing")
        sub = fa[(fa.config == r.config) & (fa.level == r.level) & (fa.msg_id == r.msg_id)]
        for k, v in store.adjusted_counts(d.diff, sub).items():
            adj[k] += v
    c = st.columns(4)
    c[0].metric("Reviewed messages", len(reviewed))
    a_f1, e_f1 = prf(auto["tp"], auto["fp"], auto["fn"])[2], prf(adj["tp"], adj["fp"], adj["fn"])[2]
    c[1].metric("Automatic critical F1", f"{a_f1:.3f}")
    c[2].metric("Expert-adjusted F1", f"{e_f1:.3f}", f"{e_f1 - a_f1:+.3f}")
    if not ma.empty:
        joined = ma.merge(results()[["config", "level", "msg_id", "escalated"]], on=["config", "level", "msg_id"])
        agree = ((joined.escalation == "should_escalate") == joined.escalated).mean()
        c[3].metric("Routing agreement", f"{agree:.0%}", help="Pipeline's escalate/auto-post decision vs the expert's")


# --- page: review ------------------------------------------------------------------------------

QUEUES = {
    "Worst first: wrong critical values": lambda q: q[q.wrong_crit > 0].sort_values("wrong_crit", ascending=False),
    "Silent failures: auto-posted but wrong": lambda q: q[(q.path == "fast") & (q.wrong_crit + q.fn_crit > 0)],
    "Random sample (unbiased calibration)": lambda q: q.sample(frac=1, random_state=7),
    "Prompt-injection messages": lambda q: q[q.corruptions.apply(lambda c: "PROMPT_INJECTION_NTE" in c)],
    "Escalated": lambda q: q[q.escalated],
}


def page_review() -> None:
    df = st.session_state.filtered
    st.title("Review queue")
    reviewer = st.session_state.get("reviewer") or ""
    if not reviewer:
        st.warning("Enter your name in the sidebar before annotating.", icon=":material/person:")

    c = st.columns([3, 2, 2, 2])
    queue_name = c[0].selectbox("Queue", list(QUEUES))
    configs = [x for x in CONFIG_ORDER if x in set(df.config)]
    config = c[1].selectbox("Config", configs)
    level = c[2].selectbox("Level", sorted(df.level.unique()), index=min(2, df.level.nunique() - 1))
    hide = c[3].toggle("Hide reviewed", value=True)

    q = QUEUES[queue_name](df[(df.config == config) & (df.level == level)])
    if hide:
        done = store.messages_df()
        done = set(done[(done.config == config) & (done.level == level)].msg_id)
        q = q[~q.msg_id.isin(done)]
    ids = q.msg_id.tolist()
    if not ids:
        st.success("Queue is empty for these filters.")
        return
    pos = {m: i for i, m in enumerate(ids)}
    advance = st.session_state.pop("advance", False)
    queue_key = (queue_name, config, level, hide)
    if st.session_state.get("queue_key") != queue_key:
        st.session_state.queue_key = queue_key
        st.session_state.pick = ids[0]
    elif st.session_state.get("pick") not in pos:
        # The message just left the queue (reviewed + hidden): the next one slid into its slot.
        st.session_state.pick = ids[min(st.session_state.get("pick_pos", 0), len(ids) - 1)]
    elif advance:
        st.session_state.pick = ids[min(pos[st.session_state.pick] + 1, len(ids) - 1)]
    st.session_state.pick_pos = pos[st.session_state.pick]

    def step(d: int) -> None:
        st.session_state.pick = ids[max(0, min(len(ids) - 1, pos[st.session_state.pick] + d))]

    n = st.columns([1, 8, 1])
    n[0].button("◀ Prev", on_click=step, args=(-1,), width="stretch")
    n[1].selectbox("Message", ids, key="pick", format_func=lambda m: f"{m}   ·   {pos[m] + 1} / {len(ids)}", label_visibility="collapsed")
    n[2].button("Next ▶", on_click=step, args=(1,), width="stretch")

    msg_id = st.session_state.pick
    row = q[q.msg_id == msg_id].iloc[0]
    d = detail(config, level, msg_id)

    k = st.columns(6)
    k[0].metric("Type", row.msg_type)
    k[1].metric("Route", "escalated" if row.escalated else "auto-post")
    k[2].metric("Valid", "yes" if row.valid else "no")
    k[3].metric("Wrong critical", int(row.wrong_crit))
    k[4].metric("Missing critical", int(row.fn_crit - row.wrong_crit))
    k[5].metric("Corruptions", len(d.manifest))

    left, right = st.columns([5, 6], gap="large")
    with left:
        st.markdown("**Incoming v2**  <span style='background:rgba(235,104,52,.28);padding:0 4px;border-radius:3px'>corrupted</span>"
                    "  <span style='border-bottom:2px dashed #2a78d6'>flagged by rules</span>  · hover a field for detail",
                    unsafe_allow_html=True)
        st.html(render_v2(d.raw, d.manifest, d.field_errors))
        with st.expander("Clean original (level 0)"):
            st.html(render_v2(d.clean, [], []))
        with st.expander(f"What was corrupted ({len(d.manifest)})", expanded=True):
            if d.manifest:
                st.dataframe(pd.DataFrame(d.manifest)[["type", "field", "before", "after"]], hide_index=True, width="stretch")
            else:
                st.caption("Nothing — clean message.")
        with st.expander(f"Rules layer flags ({len(d.field_errors)})"):
            if d.field_errors:
                st.dataframe(pd.DataFrame(d.field_errors)[["kind", "source", "target", "value", "detail"]], hide_index=True, width="stretch")
        with st.expander(f"Validator errors ({len(d.issues)})"):
            if d.issues:
                st.dataframe(pd.DataFrame(d.issues), hide_index=True, width="stretch")

    with right:
        show_matches = st.toggle("Show matching fields too", value=False)
        rows = [r for r in d.diff if show_matches or r["status"] != "match"]
        existing = store.fields_df()
        existing = existing[(existing.config == config) & (existing.level == level) & (existing.msg_id == msg_id)]
        prior = {(a.key, a.path, a.pred, a.gold): a for a in existing.itertuples()}
        grid = pd.DataFrame(rows)
        if grid.empty:
            st.success("Output matches golden on every field.")
            grid = pd.DataFrame(columns=["resource", "key", "path", "status", "critical", "pred", "gold"])
        grid[["pred", "gold"]] = grid[["pred", "gold"]].fillna("") if not grid.empty else grid[["pred", "gold"]]
        grid["verdict"] = [store.VERDICTS.get(getattr(prior.get((r.key, r.path, r.pred or "", r.gold or "")), "verdict", None), None) for r in grid.itertuples()]
        grid["severity"] = [getattr(prior.get((r.key, r.path, r.pred or "", r.gold or "")), "severity", None) for r in grid.itertuples()]
        grid["comment"] = [getattr(prior.get((r.key, r.path, r.pred or "", r.gold or "")), "comment", "") for r in grid.itertuples()]
        grid[["pred", "gold"]] = grid[["pred", "gold"]].fillna("")
        grid = grid.sort_values(["critical", "status"], ascending=[False, True])

        msg_prior = store.messages_df()
        msg_prior = msg_prior[(msg_prior.config == config) & (msg_prior.level == level) & (msg_prior.msg_id == msg_id)]
        with st.form(f"form-{config}-{level}-{msg_id}", border=False):
            st.markdown("**Field discrepancies** — is the automatic metric right?")
            edited = st.data_editor(
                grid,
                key=f"grid-{config}-{level}-{msg_id}",
                hide_index=True,
                width="stretch",
                height=min(38 * (len(grid) + 1) + 4, 520),
                disabled=["resource", "key", "path", "status", "critical", "pred", "gold"],
                column_order=["status", "critical", "resource", "path", "pred", "gold", "verdict", "severity", "comment"],
                column_config={
                    "status": st.column_config.TextColumn("status", width="small"),
                    "critical": st.column_config.CheckboxColumn("crit", width="small"),
                    "path": st.column_config.TextColumn("FHIR path", width="medium"),
                    "pred": st.column_config.TextColumn("pipeline wrote"),
                    "gold": st.column_config.TextColumn("golden"),
                    "verdict": st.column_config.SelectboxColumn("verdict", options=VERDICT_LABELS, width="medium"),
                    "severity": st.column_config.SelectboxColumn("clinical severity", options=store.SEVERITIES, width="small"),
                    "comment": st.column_config.TextColumn("comment"),
                },
            )
            st.markdown("**Routing** — should this message have gone to a human?")
            esc_now = store.ESCALATION.get(msg_prior.escalation.iloc[0]) if not msg_prior.empty else None
            esc = st.radio("routing", ESC_LABELS, index=ESC_LABELS.index(esc_now) if esc_now else None, horizontal=True, label_visibility="collapsed")
            comment = st.text_area("Message comment", value=msg_prior.comment.iloc[0] if not msg_prior.empty else "", height=70)
            b = st.columns([1, 1, 2])
            fill = b[2].checkbox("Unmarked critical rows → pipeline error", help="Bulk-mark every critical discrepancy you left blank")
            save = b[0].form_submit_button("Save", width="stretch", disabled=not reviewer)
            save_next = b[1].form_submit_button("Save & next", type="primary", width="stretch", disabled=not reviewer)

        if save or save_next:
            recs = edited.to_dict("records")
            for r in recs:
                if fill and not r.get("verdict") and r["critical"] and r["status"] != "match":
                    r["verdict"] = store.VERDICTS["pipeline_error"]
                r["verdict"] = LABEL_TO_VERDICT.get(r.get("verdict")) if r.get("verdict") else None
            n_saved = store.save_fields(config, level, msg_id, recs, reviewer)
            if esc:
                store.save_message(config, level, msg_id, LABEL_TO_ESC[esc], comment, reviewer)
            st.toast(f"Saved {n_saved} field verdicts" + (" + routing" if esc else ""), icon=":material/check:")
            if save_next:
                st.session_state.advance = True
            st.rerun()

    with st.expander("Bundles (JSON)"):
        j = st.columns(2)
        j[0].markdown("**Pipeline output**")
        j[0].json(d.bundle or {}, expanded=False)
        j[1].markdown("**Golden**")
        j[1].json(d.golden, expanded=False)


# --- page: insights ----------------------------------------------------------------------------

def page_insights() -> None:
    st.title("Expert insights")
    fa, ma = store.fields_df(), store.messages_df()
    if fa.empty:
        st.info("No field annotations yet.")
        return
    fa["verdict_label"] = fa.verdict.map(store.VERDICTS)
    c = st.columns(4)
    c[0].metric("Field verdicts", len(fa))
    c[1].metric("Messages reviewed", fa[["config", "level", "msg_id"]].drop_duplicates().shape[0])
    c[2].metric("Metric too strict", f"{(fa.verdict == 'equivalent').mean():.0%}", help="Share of discrepancies experts called clinically equivalent")
    c[3].metric("Patient-safety errors", int(((fa.verdict == "pipeline_error") & (fa.severity == "patient-safety")).sum()))

    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("**Verdicts by discrepancy type**")
        v = fa.groupby(["status", "verdict_label"]).size().rename("n").reset_index()
        st.altair_chart(
            alt.Chart(v).mark_bar(cornerRadiusEnd=4).encode(
                y=alt.Y("verdict_label:N", title=None), x=alt.X("n:Q", title="field discrepancies"),
                color=alt.Color("status:N", scale=alt.Scale(domain=["wrong", "missing", "extra"], range=["#2a78d6", "#eb6834", "#1baf7a"]),
                                legend=alt.Legend(orient="bottom")),
                tooltip=["verdict_label", "status", "n"],
            ).properties(height=220),
            width="stretch",
        )
    with right:
        st.markdown("**Severity of confirmed pipeline errors**")
        s = fa[fa.verdict == "pipeline_error"].groupby("severity").size().reindex(store.SEVERITIES, fill_value=0).rename("n").reset_index()
        st.altair_chart(
            alt.Chart(s).mark_bar(cornerRadiusEnd=4, color="#2a78d6").encode(
                x=alt.X("severity:N", sort=store.SEVERITIES, title=None), y=alt.Y("n:Q", title="fields"), tooltip=["severity", "n"]
            ).properties(height=220),
            width="stretch",
        )

    st.markdown("**Verdicts by injected corruption**")
    res = results()[["config", "level", "msg_id", "corruptions"]]
    j = fa.merge(res, on=["config", "level", "msg_id"]).explode("corruptions").dropna(subset=["corruptions"])
    if not j.empty:
        m = j.groupby(["corruptions", "verdict_label"]).size().rename("n").reset_index()
        heat = alt.Chart(m).encode(x=alt.X("verdict_label:N", title=None), y=alt.Y("corruptions:N", title=None), tooltip=["corruptions", "verdict_label", "n"])
        st.altair_chart(
            (heat.mark_rect(cornerRadius=3).encode(color=alt.Color("n:Q", scale=alt.Scale(range=["#cde2fb", "#104281"]), legend=None))
             + heat.mark_text(fontSize=11).encode(text="n:Q")).properties(height=300),
            width="stretch",
        )

    st.markdown("**Normalizer candidates** — paths experts keep calling equivalent. Fix these in the scorer or the rules layer, not with an agent.")
    eq = fa[fa.verdict == "equivalent"].groupby(["path", "pred", "gold"]).size().rename("times").reset_index().sort_values("times", ascending=False)
    st.dataframe(eq, hide_index=True, width="stretch")

    with st.expander("All annotations"):
        st.dataframe(fa, hide_index=True, width="stretch")
    b = st.columns(2)
    b[0].download_button("Field annotations (CSV)", fa.to_csv(index=False), "field_annotations.csv", "text/csv")
    b[1].download_button("Message annotations (CSV)", ma.to_csv(index=False), "message_annotations.csv", "text/csv")
