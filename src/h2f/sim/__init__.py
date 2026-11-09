"""NICU FHIR simulation: catalog-driven forms -> validating middleware -> HAPI.

See docs/sim.md. Layers: catalog.py (spec as data) -> auth.py (identity) -> checks.py (clinical/state)
-> mapping.py (FHIR) -> pipeline.py (orchestration) -> api.py (FastAPI) -> ui.py (Streamlit).
"""
