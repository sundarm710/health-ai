"""Streamlit renderers. Each page imports ONLY its own service layer + `charts`; pages never import each other.

    client.py    the NICU form simulator   -> talks HTTP to the middleware (sim API)
    server.py    request logs & latency    -> fhirlab.obs  (LogStore, analytics, probe)
    database.py  HAPI Postgres explorer    -> fhirlab.dbview (DbRepo, service)
    charts.py    DataFrame -> Vega-Lite chart (Altair); portable to any Vega front end via chart.to_dict()
"""
