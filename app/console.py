"""Console for the NICU FHIR simulation.  Run: `make sim`  (or `uv run streamlit run app/console.py --server.port 8502`)

This file is only the shell: it wires concrete adapters to three independent pages and owns navigation.
Each page can be lifted out (or replaced by a React app talking to fhirlab.core.obs.api / fhirlab.core.db.api) without touching the others.
"""

import os

import streamlit as st

from fhirlab.core.db import PgRepo
from fhirlab.core.obs import JsonlLogStore
from fhirlab.console.pages.client import API, client_page
from fhirlab.console.pages.database import database_page
from fhirlab.console.pages.server import server_page

st.set_page_config(layout="wide", page_title="NICU FHIR console", page_icon=":material/child_care:")

HAPI = os.environ.get("HAPI_BASE", "http://localhost:8080/fhir")
pg = st.navigation(
    [
        st.Page(client_page, title="Client", icon=":material/person:", url_path="client", default=True),
        st.Page(lambda: server_page(JsonlLogStore(), middleware_url=st.session_state.get("api_base", API), hapi_url=HAPI),
                title="Server", icon=":material/dns:", url_path="server"),
        st.Page(lambda: database_page(PgRepo()), title="Database", icon=":material/database:", url_path="database"),
    ]
)
pg.run()
