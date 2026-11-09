# health-ai: HL7v2 → FHIR, and a NICU simulation on a real FHIR server

Two connected pieces of one project. Everything uses **synthetic data only**.

1. **HL7v2 → FHIR mapping pipeline.** Rules map well-formed messages; agents (in progress) handle only the malformed tail.
   A corpus of 2,533 messages from 500 Synthea patients at five nested corruption levels, a YAML rules mapper whose clean output equals the golden set exactly,
   an eval harness, and an expert-review UI.
2. **NICU simulation.** A clinician's form → middleware (authorise · check · map) → FHIR transaction → HAPI → Postgres,
   with a console to watch every hop: **Client**, **Server** and **Database** pages.

```
 Streamlit console ──HTTP──▶ FastAPI middleware ──HTTP──▶ HAPI FHIR ──SQL──▶ Postgres 17
 (Client page)               (fhirlab.sim.api)                 :8080              :5432
        │                          │ writes JSONL request log                      ▲
        ▼                          ▼                                               │
   Server page  ◀── fhirlab.obs ◀── data/logs/middleware.jsonl        Database page ◀── fhirlab.dbview
```

## Quick start
Needs Docker, [uv](https://docs.astral.sh/uv/), Python 3.12 (Java 17 only for `make synthea`).

```bash
make up         # Postgres + HAPI (+ Adminer on :8081); waits until HAPI answers (first start is slow)
make sim-seed   # once: 1 hospital, 2 doctors, 2 nurses (idempotent)
make sim-api    # terminal 1: middleware on :8000 (docs at /docs)
make sim        # terminal 2: console on :8502  → Client · Server · Database
make test       # run after every change
```

| Port | What |
|---|---|
| 8502 | Console (Client · Server · Database) |
| 8000 | Middleware API (`/docs`, plus `/ops/*` read-only metrics) |
| 8080 | HAPI FHIR (`/fhir`) |
| 8081 | Adminer (raw table browser; `make db-ui`) |
| 5432 | Postgres (`make psql`) |
| 8501 | Expert review UI (`make review`) |

Other targets: `down` · `reset` (deletes all FHIR data) · `demo` (narrated FHIR REST tour) · `synthea` · `corpus` · `baseline` · `charts`.

## The console
- **Client:** the NICU form simulator. Pick role, practitioner, operation and baby as filters; fill the form; **Validate** then **Submit**. Results stay pinned beside the form.
- **Server:** the middleware's own request log: health (rate, errors, p50/p95), a filterable request table with a waterfall of each request's HAPI calls, and where time goes.
- **Database:** HAPI's Postgres, read-only: a per-patient timeline of every stored version with diffs, the resource landscape, and a guarded SQL box.

A request id ties it together: the Client trace shows it, the Server log records it, and HAPI stores it on the rows it writes (`hfj_res_ver.request_id`).

## Layout
```
src/fhirlab/
  corpus/ parse/ rules/ validate/ eval/ review/ pipeline.py   HL7v2→FHIR: corpus, tolerant parser, YAML rules, scoring, review store
  agents/                                                     the agentic layer (next)
  client/        FhirClient: the one thin FHIR REST client (+ CLI)
  sim/           NICU middleware: catalog (YAML forms), authz, checks, mapping, pipeline, API
  obs/           request logging middleware, LogStore, analytics, probes     (no UI)
  dbview/        DbRepo (SQL), service (timelines, diffs)                    (no UI)
  ui/            Streamlit renderers: client.py, server.py, database.py, charts.py
app/             Streamlit entrypoints (thin shells that wire adapters to pages)
tests/           pytest; live tests skip themselves when the stack is down
docs/            console.md · sim.md · fhir-server-guide.md · summary.md
```

## Docs
- [`docs/console.md`](docs/console.md): console architecture, how to swap Streamlit for another front end, server logging best practices.
- [`docs/sim.md`](docs/sim.md): the NICU simulation: forms, rules, what each form becomes in FHIR.
- [`docs/fhir-server-guide.md`](docs/fhir-server-guide.md): FHIR REST interactions and HAPI/Postgres internals, with exercises.
- [`docs/summary.md`](docs/summary.md): eval results for the rules-only baseline.

Contributing (workflow, layering rules, commit discipline): see [`CLAUDE.md`](CLAUDE.md).
