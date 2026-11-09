# CLAUDE.md

HL7v2→FHIR mapping pipeline + a NICU simulation on a local HAPI FHIR server (Postgres-backed). Synthetic data only. Python 3.12, `uv`, pytest, Streamlit, FastAPI.
The learning goal: depth goes into the **agentic layer** (`src/h2f/agents/`: mapper, repair, terminology, reconciler, evals). Corpus/rules/UI scaffolding should be built quickly and kept simple.

## Commands
```
uv run pytest -q                         # whole suite (~20 s). Run after EVERY build step
uv run pytest tests/test_obs.py -q       # one file;  add ::test_name for one test
make up | down | reset                   # Postgres + HAPI (reset DELETES all FHIR data: never run unasked)
make sim-seed | sim-api | sim            # seed practitioners | middleware :8000 | console :8502
make demo | psql | db-ui                 # FHIR tour | SQL shell | Adminer
```
Live tests (`-k live`, `TestLive`) skip themselves when HAPI/Postgres are down. A skipped live test is not a pass: if you touched code they cover, run `make up` and re-run them.

## How to work (in this order, every time)
1. **Build the smallest slice** that can be verified on its own. One layer, one behavior. Do not stack three unverified pieces.
2. **Test it immediately.** Write the test with the code, at the lowest layer that can show the behavior (pure function > service with a fake > API with `TestClient` > Streamlit `AppTest`). Then run the **full** suite, not just the new file.
3. **Run the real thing** when the change touches a boundary (HTTP, SQL, HAPI, UI). Tests with fakes prove logic; only a live run proves the wiring. For charts, render to PNG with `uv run --with vl-convert-python` (kept out of the project deps) and *look* at it, since tests cannot catch overlapping labels.
4. **Commit** (see below), then start the next slice.
If a test fails, find the cause. Never weaken, skip or delete a test to get green. If you cannot verify something (no browser, stack down), say so in your report; do not imply it was checked.

## Separation of concerns (the layering is the architecture)
Dependencies point **down** only: `ui` → `service/analytics` → `repo/store` (I/O) ← adapters. Never up, never sideways.

| Layer | Owns | Must NOT import |
|---|---|---|
| `obs/record.py`, `dbview/service.py`, `obs/analytics.py` | contracts, pure transforms (pandas) | streamlit, httpx, psycopg, files |
| `obs/store.py`, `dbview/repo.py`, `client/` | **all** I/O: files, SQL, HTTP to HAPI | streamlit, charts |
| `obs/middleware.py`, `*/api.py` | ASGI/FastAPI glue | streamlit |
| `ui/charts.py` | DataFrame → Altair spec | data access, streamlit |
| `ui/client.py`, `ui/server.py`, `ui/database.py` | Streamlit rendering for **one** page | each other; raw SQL; file reads |
| `app/*.py` | wiring only: build adapters, pass them to pages, navigation | logic |

- Pages receive their data source as an argument (`server_page(store, …)`, `database_page(repo)`); they never construct it.
- Ports are `Protocol`s (`LogStore`, `DbRepo`). Fakes in tests implement the same Protocol.
- The **middleware is the only writer** to HAPI, through one transaction Bundle with `ifMatch`/`ifNoneExist`. The DB layer is read-only (session read-only, SELECT-only guard).
- Business rules live in **data** (`sim/catalog/*.yaml`, `rules/templates/*.yaml`, `UNIT_POLICY`), not scattered in UI code. If the UI needs a rule, the server sends it (see how `unit_policy` reaches the form's `*` markers); do not copy the rule.

## Keep the repo convergent
- **One way to do each thing.** FHIR calls go through `FhirClient`; charts through `ui/charts.py`; forms through the catalog; logging through `h2f.obs`. Before adding a helper, dependency or pattern, search for the existing one and extend it.
- **Single source of truth.** If two places must agree, one derives from the other (or tests assert they match). Duplicated constants/logic drift.
- **Delete what you supersede** in the same change (old code path, stale doc line, dead test). No `_v2`, no parallel implementations, no commented-out code.
- **Reuse before create.** New module only when a new *layer or concern* appears; otherwise add to the module that owns it. Match existing naming and style.
- **Docs move with code.** A behavior/port/command change updates `README.md` and the relevant `docs/*.md` in the same commit.
- Add a dependency only if nothing present does the job; `uv add`, keep `pyproject.toml` and `uv.lock` together.

## Commits: regular, isolated, green
- **One logical change per commit.** Refactor, feature, data fix, docs and config are separate commits. Never mix a rename/move with a behavior change.
- **Every commit passes `uv run pytest -q`** on its own and leaves the repo runnable. Commit as soon as a slice is verified; do not batch a session into one commit.
- Stage explicit paths (`git add path…`), never `git add -A`; read `git diff --staged` first. Work on a branch off `main`, not on `main`.
- Message: imperative subject ≤ 72 chars, a body saying *why*. End with the attribution trailer the harness specifies.
- **Never commit:** `data/`, `results/`, `.env`, `tools/*.jar`, `.venv/`, logs. (`annotations/annotations.sqlite` is the reviewers' work product: commit only deliberately.)
- Only commit/push when asked, or when the user has said to commit regularly; never force-push, never rewrite published history, never `reset --hard` without asking.

## Domain rules and gotchas (each cost real time)
- **Health data hygiene:** logs and metrics carry route templates, resource *types*, outcomes and timings: never bodies, query values or patient ids. Synthetic only; still build as if real.
- HAPI image is **distroless** (no curl/sh): health checks poll from the host (`make up`). First start takes minutes.
- HAPI **caches identical searches for 60 s**: state-check lookups send `Cache-Control: no-cache` (`sim/state.py FRESH`). Stale reads are correctness bugs.
- Stale `If-Match` returns **409** from HAPI (spec says 412). Handle both.
- HAPI stores our `X-Request-ID` in `hfj_res_ver.request_id` only because of `hapi.fhir.store_meta_source_information` in `docker-compose.yml`. Request ids are ≤16 alphanumerics.
- DB timestamps are **UTC, naive**. Convert to IST (`Asia/Kolkata`) only at display time, in the UI layer.
- Names are denormalised: a patient's name also lives in `Encounter.subject.display` and `RelatedPerson.patient.display`. Update all three in one transaction (the baby dropdown reads the Encounter).
- Fixing existing FHIR data = FHIR `PUT` + `If-Match` in a transaction (creates a version, keeps history). **Never `UPDATE` HAPI tables with SQL.**
- Streamlit: `st.set_page_config` once, in the entrypoint. Test pages with `AppTest.from_function`; give widgets stable `key=`s. Test data must not leak into real logs (`tests/conftest.py` redirects `H2F_LOG_PATH`).
- Altair 6: no nested `alt.condition`; compute a column and use a scale. Never dual-axis charts; fixed-pixel row heights (`_step`) for category charts; categorical colours in fixed order, status red/amber reserved for failure.
- pandas 3: `None` in object columns becomes `NaN`: test with `isinstance(x, str)`, not truthiness.
- Java for Synthea: `/opt/homebrew/opt/openjdk@17/bin/java` (brew formula; the cask needs sudo).
