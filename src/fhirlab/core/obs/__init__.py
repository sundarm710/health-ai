"""Server observability, UI-free.

    record.py      the contract: one RequestLog per HTTP request (JSON-serialisable)
    middleware.py  ASGI middleware that produces RequestLogs (the only piece that runs *inside* the server)
    store.py       LogStore port + JSONL adapter (swap for Loki/Elastic/SQLite without touching anything else)
    analytics.py   pure pandas: RequestLogs -> KPIs, series, breakdowns (the "view models")
    probe.py       active latency probes (what a monitor sees from outside)
    api.py         optional HTTP router exposing the view models as JSON (for a React/Grafana front end)

Nothing here imports Streamlit.
"""

from fhirlab.core.obs.middleware import RequestLogMiddleware, annotate
from fhirlab.core.obs.record import RequestLog, Upstream
from fhirlab.core.obs.store import JsonlLogStore, JsonlSink, LogStore

__all__ = ["RequestLogMiddleware", "annotate", "RequestLog", "Upstream", "LogStore", "JsonlLogStore", "JsonlSink"]
