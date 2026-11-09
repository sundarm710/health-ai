"""Headless smoke tests for the review app pages (Streamlit AppTest)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

pytestmark = pytest.mark.skipif(not Path("results/rules_only.parquet").exists(), reason="run `make baseline` first")


def _page(name: str):
    def script(page_name: str, db: str):
        import streamlit as st

        from fhirlab.clients.hl7v2.review import store, ui

        store.DB = __import__("pathlib").Path(db)
        st.session_state.reviewer = "tester"
        st.session_state.filtered = ui.sidebar(ui.results())
        getattr(ui, page_name)()

    return script


def _run(page: str, db: Path) -> AppTest:
    at = AppTest.from_function(_page(page), args=(page, str(db)), default_timeout=180)
    return at.run()


def test_pages_render_and_annotations_flow(tmp_path):
    from fhirlab.clients.hl7v2.review import data, store

    db = tmp_path / "ann.sqlite"
    at = _run("page_review", db)
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "Review queue"

    # Simulate an expert saving verdicts for the message on screen.
    msg_id = at.session_state["pick"]
    d = data.load_detail("rules_only", 0.4, msg_id)
    disc = [r for r in d.diff if r["status"] != "match" and r["critical"]]
    assert disc, "worst-first queue should open on a message with critical discrepancies"
    store.DB = db
    rows = [{**r, "verdict": "equivalent" if i == 0 else "pipeline_error", "severity": "major"} for i, r in enumerate(disc)]
    assert store.save_fields("rules_only", 0.4, msg_id, rows, "tester") == len(disc)
    store.save_message("rules_only", 0.4, msg_id, "should_escalate", "", "tester")

    # Adjusted F1 must move in the expected direction: one discrepancy forgiven.
    ann = store.fields_df()
    auto = {"tp": sum(r["status"] == "match" and r["critical"] for r in d.diff)}
    adj = store.adjusted_counts(d.diff, ann)
    assert adj["tp"] == auto["tp"] + 1

    for page in ("page_trends", "page_insights"):
        at = _run(page, db)
        assert not at.exception, (page, [e.value for e in at.exception])
    labels = [m.label for m in at.metric]
    assert "Field verdicts" in labels
