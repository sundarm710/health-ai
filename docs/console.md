# The console: Client · Server · Database

`make sim` → http://localhost:8502. Three pages, three independent modules.

```
 app/sim_app.py   (shell: wires adapters, owns navigation, nothing else)
   ├─ Client    ui/client.py    ──HTTP──▶ middleware (sim API)
   ├─ Server    ui/server.py    ──▶ obs.LogStore ─▶ obs.analytics ─▶ ui/charts.py
   └─ Database  ui/database.py  ──▶ dbview.DbRepo ─▶ dbview.service ─▶ ui/charts.py
```

| Layer | Knows about | Does not know about |
|---|---|---|
| `obs/record.py` | the log contract | everything else |
| `obs/middleware.py` | ASGI, contextvars | files, pandas, Streamlit |
| `obs/store.py`, `dbview/repo.py` | files / SQL (the only I/O) | charts, Streamlit |
| `obs/analytics.py`, `dbview/service.py` | pandas | I/O, Streamlit |
| `obs/api.py`, `dbview/api.py` | FastAPI | Streamlit |
| `ui/charts.py` | Altair (= Vega-Lite JSON) | data access |
| `ui/{client,server,database}.py` | Streamlit + one service layer | each other |

**Moving off Streamlit:** a React app calls `/ops/*` (mounted on the middleware) and `dbview.api` (`uvicorn fhirlab.dbview.api:app --port 8001`),
and renders `chart.to_dict()` specs with `react-vega`, or its own charts from the JSON.
**Swapping a backend:** implement `LogStore` (Loki, Elastic, SQLite) or `DbRepo`; nothing above changes.

## Server: what we log and why (the learning part)
1. **One structured line per request, JSON, no free text.** Greppable (`jq`), shippable, aggregatable. `data/logs/middleware.jsonl`.
2. **Log the route *template*, never the raw path** (`/patients/{pid}`): bounded cardinality, and no ids in logs.
3. **Never log bodies, query values or patient ids** in a health system. Log shapes, outcomes, timings.
4. **Request id on every hop.** Generated at the edge, returned as `X-Request-ID`, forwarded to HAPI, which now stores it in `hfj_res_ver.request_id`
   (`store_meta_source_information` in docker-compose). Client trace → server log → DB row share one key.
5. **RED per route**: Rate, Errors, Duration. **Percentiles (p50/p95/p99), never averages.**
6. **Categorise by cause, not status code**: `conflict` (409) and `validation` (422) are both 4xx and need opposite reactions; `upstream_error` ≠ `server_error`.
7. **Separate "own time" from "waiting on a dependency"** (every HAPI call is timed): tells you which code to fix.
8. **Probes complement logs**: logs describe real traffic; probes work at 3 a.m. with no traffic and split connect cost from request cost.
9. **Logging must never break a request** (the sink is wrapped); a torn line must not hide the rest.
10. Next steps for production: rotation/shipping, OpenTelemetry traces (`traceparent`), SLOs with burn-rate alerts.

## The three lenses per page
| Server | Question | Database | Question |
|---|---|---|---|
| Pulse | healthy right now? | Patient timeline | what happened to this baby, in order? |
| Requests | what happened to *that* request? | Landscape | what is in here / who references whom? |
| Dependencies | where does the time go? | Tables & SQL | show me the actual rows |
