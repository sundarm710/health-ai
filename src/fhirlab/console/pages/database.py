"""Database page: a read-only window into HAPI's Postgres.

Depends on `fhirlab.core.db` (DbRepo + service) and `charts`. Three lenses:
    Patient timeline  "what happened to this baby, in order?"   swim-lane of every stored version + field-level diff
    Landscape         "what is in here?"                        resource mix, write activity, who-references-whom
    Tables & SQL      "show me the actual rows"                 table browser and a guarded read-only SQL box
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from fhirlab.core.db import UnsafeSql, service
from fhirlab.core.db.repo import DbRepo
from fhirlab.console import charts

DISPLAY_TZ = "Asia/Kolkata"
PRESETS = {
    "Latest 10 versions written": "select res_type, res_id, res_ver, res_updated, request_id from hfj_res_ver order by res_updated desc limit 10",
    "Rows stamped with a middleware request id": "select res_type, res_id, res_ver, request_id, res_updated from hfj_res_ver where request_id is not null order by res_updated desc",
    "Most-edited resources": "select res_type, res_id, max(res_ver) as versions from hfj_res_ver group by 1, 2 order by 3 desc limit 10",
    "What is in the search index for one type": "select sp_name, count(*) from hfj_spidx_token group by 1 order by 2 desc limit 15",
    "Biggest tables": "select relname, pg_size_pretty(pg_total_relation_size(relid)) as size from pg_stat_user_tables order by pg_total_relation_size(relid) desc limit 10",
}


def database_page(repo: DbRepo) -> None:
    st.title("Database")
    st.caption("HAPI's Postgres, read-only. Every FHIR resource is stored as JSON in `hfj_res_ver` (one row per version); `hfj_resource` points at the current one.")
    try:
        repo.tables()
    except Exception as e:  # connection errors come in many types; show them, don't crash the app
        st.error(f"Cannot reach Postgres: {type(e).__name__}. Run `make up`.")
        return
    t_tl, t_land, t_tab = st.tabs([":material/timeline: Patient timeline", ":material/hub: Landscape", ":material/table: Tables & SQL"])
    with t_tl:
        _timeline(repo)
    with t_land:
        _landscape(repo)
    with t_tab:
        _tables(repo)


def _local(s: pd.Series) -> pd.Series:
    return s.dt.tz_convert(DISPLAY_TZ).dt.tz_localize(None)


# ----------------------------------------------------------------------------------------------- lens 1
def _timeline(repo: DbRepo) -> None:
    pats = repo.patients()
    if pats.empty:
        st.info("No patients yet. Admit a baby on the Client page, or `make sim-seed` / `make demo`.")
        return
    labels = {int(r.res_id): f"{r['name'] or '(unnamed)'} · Patient/{r.fhir_id} · {r.resources} resources · {r.versions} versions" for _, r in pats.iterrows()}
    pid = st.selectbox("Patient", list(labels), format_func=labels.get)
    tl = service.patient_timeline(repo, int(pid))
    bodies = tl.attrs.get("bodies", {})
    if tl.empty:
        st.info("No history found.")
        return
    tl = tl.assign(when=_local(tl["when"]))
    types = st.pills("Resource types", sorted(tl["res_type"].unique()), selection_mode="multi", default=sorted(tl["res_type"].unique()))
    tl = tl[tl["res_type"].isin(types)]
    m = st.columns(4)
    m[0].metric("Events", len(tl), border=True)
    m[1].metric("Resources", tl["res_id"].nunique(), border=True)
    m[2].metric("Edits", int((tl["kind"] == "updated").sum()), border=True, help="Versions beyond the first: someone changed an existing resource.")
    m[3].metric("Span", f"{(tl['when'].max() - tl['when'].min()).total_seconds() / 3600:.1f} h" if len(tl) > 1 else "—", border=True)
    st.altair_chart(charts.timeline(tl), width="stretch")
    st.caption("One dot per stored version. Compartment = the patient, everything that references them, and the Provenance records about those.")
    show = tl.sort_values("when", ascending=False)[["when", "res_type", "fhir_id", "res_ver", "kind", "summary", "request_id", "res_id"]]
    ev = st.dataframe(show, hide_index=True, width="stretch", height=260, on_select="rerun", selection_mode="single-row",
                      column_config={"when": st.column_config.DatetimeColumn("time (IST)", format="YYYY-MM-DD HH:mm:ss"), "res_id": None})
    if not ev.selection.rows:
        st.info("Select an event to see the stored JSON and what changed from the previous version.")
        return
    row = show.iloc[ev.selection.rows[0]]
    rid, ver = int(row["res_id"]), int(row["res_ver"])
    cur, prev = bodies.get(f"{rid}:{ver}"), bodies.get(f"{rid}:{ver - 1}")
    l, r = st.columns(2)
    with l:
        st.markdown(f"##### {row['res_type']}/{row['fhir_id']} · v{ver}")
        st.code(_pretty(cur), language="json", height=320)
    with r:
        st.markdown(f"##### Change from v{ver - 1}" if prev else "##### First version")
        if prev or ver == 1:
            st.dataframe(service.diff_versions(prev, cur), hide_index=True, width="stretch")
        if row["request_id"]:
            st.caption(f"Written by middleware request `{row['request_id']}`: look it up on the **Server** page.")


def _pretty(body) -> str:
    import json

    return json.dumps(json.loads(body), indent=2) if isinstance(body, str) else "(deleted: HAPI keeps a marker row, no body)"


# ----------------------------------------------------------------------------------------------- lens 2
def _landscape(repo: DbRepo) -> None:
    counts = repo.resource_counts()
    k = st.columns(4)
    k[0].metric("Resource types", len(counts), border=True)
    k[1].metric("Live resources", int(counts["resources"].sum()), border=True)
    k[2].metric("Stored versions", int(counts["versions"].sum()), border=True)
    k[3].metric("Deleted", int(counts["deleted"].sum()), border=True)
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown("##### What is stored")
        st.altair_chart(charts.resource_counts(counts), width="stretch")
        st.caption("versions > resources means edits (FHIR never overwrites; it appends a version).")
    with b:
        freq = st.segmented_control("Bucket", ["minute", "hour", "day"], default="hour", key="act_freq") or "hour"
        st.markdown("##### Write activity")
        act = repo.activity(freq)
        act = act.assign(ts=_local(pd.to_datetime(act["ts"], utc=True))) if not act.empty else act
        st.altair_chart(charts.activity(act), width="stretch")
    st.space("large")
    st.markdown("##### Who references whom")
    st.altair_chart(charts.link_heatmap(repo.link_matrix()), width="stretch")
    st.caption("Built from `hfj_res_link`. This is how HAPI answers “everything for this patient” without scanning JSON.")


# ----------------------------------------------------------------------------------------------- lens 3
def _tables(repo: DbRepo) -> None:
    tabs = repo.tables()
    f1, f2 = st.columns([1, 2], vertical_alignment="bottom")
    kinds = f1.pills("Kind", sorted(tabs["kind"].unique()), selection_mode="multi", default=["hapi-core"])
    show = tabs[tabs["kind"].isin(kinds)] if kinds else tabs
    pick = f2.selectbox("Table", list(show["name"]), index=0 if len(show) else None)
    if pick:
        cols, size = repo.table_columns(pick), int(tabs.set_index("name").loc[pick, "size_bytes"])
        st.caption(f"~{int(tabs.set_index('name').loc[pick, 'approx_rows']):,} rows · {size / 1024:,.0f} KiB on disk · {len(cols)} columns")
        c1, c2 = st.columns([1, 3])
        c1.dataframe(cols, hide_index=True, width="stretch", height=300)
        page = c2.number_input("Page (100 rows)", min_value=1, value=1, step=1)
        c2.dataframe(repo.table_rows(pick, 100, (int(page) - 1) * 100), hide_index=True, width="stretch", height=300)
    st.markdown("##### Ask your own question")
    st.caption("Read-only session, 5 s timeout, one SELECT/WITH/EXPLAIN at a time, 200-row cap.")
    preset = st.selectbox("Start from", ["(blank)", *PRESETS])
    q = st.text_area("SQL", value=PRESETS.get(preset, ""), height=110, key=f"sql_{preset}")
    if st.button("Run", type="primary", icon=":material/play_arrow:") and q.strip():
        try:
            res = repo.run_sql(q)
            st.dataframe(res, hide_index=True, width="stretch")
            st.caption(f"{len(res)} row(s)")
        except UnsafeSql as e:
            st.error(str(e))
