"""Console: Streamlit frontend over the core and the NICU client. Entrypoint: app/console.py.

    pages/client.py    NICU form simulator   -> HTTP to the NICU middleware
    pages/server.py    request logs/latency  -> core.obs
    pages/database.py  HAPI Postgres explorer -> core.db
    charts.py          DataFrame -> Altair (Vega-Lite) chart
Pages never import each other; they receive their data source as an argument.
"""
