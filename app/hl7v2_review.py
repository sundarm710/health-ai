"""Expert review layer for the HL7v2 -> FHIR pipeline.

    uv run streamlit run app/hl7v2_review.py
"""

import streamlit as st

from fhirlab.clients.hl7v2.review.ui import page_insights, page_review, page_trends, results, sidebar

st.set_page_config(page_title="HL7→FHIR Review", page_icon=":material/rate_review:", layout="wide")

st.session_state.filtered = sidebar(results())
pg = st.navigation(
    [
        st.Page(page_trends, title="Trends", icon=":material/monitoring:", default=True),
        st.Page(page_review, title="Review queue", icon=":material/rate_review:"),
        st.Page(page_insights, title="Expert insights", icon=":material/insights:"),
    ]
)
pg.run()
