# Tiered guide: every service, three ways

Every component below is explained three times, at three depths. Read the voice that fits you; skim the others for contrast.

| Voice | For | Style |
|---|---|---|
| **ELI5** | someone who has never written code | one analogy, no jargon |
| **Newcomer** | a sharp beginner entering backend and FHIR | what it is, how it works, the words to learn |
| **Veteran** | 20+ years of industry experience | design decisions, trade-offs, failure modes, what production demands |

Each voice covers the same five things: **what & why · how it works · alternatives · known gaps · path to production**.

## The map

```
                      ┌─────────────── console (Streamlit) ───────────────┐
                      │  Client page       Server page     Database page  │
                      └────┬──────────────────┬────────────────┬──────────┘
                           │ HTTP             │ reads file     │ SQL (read-only)
              ┌────────────▼───────┐   ┌──────▼─────┐          │
   clients    │ NICU middleware    │──▶│ request log│          │
              │ (FastAPI)          │   │ (JSONL)    │          │
              └────────────┬───────┘   └────────────┘          │
              ┌────────────┴───────┐                           │
              │ HL7v2 batch client │ (scripts; talks to HAPI directly)
              └────────────┬───────┘                           │
   ═══════════════════════ │ ══════════════════════════════════│═══════
   core                    ▼                                   ▼
                    FhirClient ──HTTP──▶ HAPI FHIR server ──SQL──▶ Postgres 17
                                         (:8080/fhir)              (:5432)      Adminer (:8081)
```

Rule: arrows only point at the core. `tests/test_architecture.py` fails the build if a client imports another client, or the core imports anything above it.

Terms used throughout: **FHIR** is the standard shape for health records (Patient, Observation, Encounter…). **HAPI** is an open-source server that stores and serves FHIR. **HL7v2** is the older pipe-delimited message format hospitals still use everywhere. **PHI** is protected health information.

---

## 1. Postgres: the storage

### ELI5
It is the filing cabinet. Every note about every baby ends up in a drawer here. Nobody talks to the cabinet directly except the clerk (HAPI), who knows where everything goes.
- *Why:* so nothing is lost when the computer is turned off.
- *Other cabinets:* there are other brands, but this one is popular and free.
- *What's missing:* the cabinet is not locked (the password is "hapi"), there is only one copy, and nobody takes backups.
- *To be real:* a locked cabinet, copies in another building, and a nightly photo of every drawer.

### Newcomer
- **What:** PostgreSQL 17 in Docker (`docker-compose.yml`), volume `pgdata`, port 5432. HAPI uses it through JPA/Hibernate; you never write to it by hand.
- **How it stores FHIR:** not as one table per resource type. `hfj_resource` has one row per resource (type, id, current version). `hfj_res_ver` has one row per *version* with the JSON in `res_text_vc`. Search works through side tables, `hfj_spidx_*` (one per search-parameter type: string, token, date…), and `hfj_res_link` stores references between resources. FHIR never overwrites: an edit adds a version.
- **Why that matters:** if you edit the JSON with SQL, the search tables go stale and searches silently return wrong answers. Always write through HAPI.
- **Alternatives:** MongoDB (older FHIR servers), cloud FHIR stores (Google Cloud Healthcare API, AWS HealthLake, Azure Health Data Services) that hide the database, or HAPI on Oracle/SQL Server.
- **Gaps:** credentials in the compose file, one node, no backup, no migrations story, default tuning, port published to the host.
- **To production:** managed Postgres (RDS/Cloud SQL) with automated backups, point-in-time recovery, a read replica for the dashboards, TLS, secrets from a vault.

### Veteran
- **Decision:** HAPI JPA starter on Postgres, `HapiFhirPostgresDialect`, Hibernate-managed schema. Chosen because the data model is append-only and relational enough that Postgres is the least surprising store; JSON stays in `res_text_vc` (text) rather than a LOB (`res_text`, an OID), which is the right default for inspection and for `jsonb` casts in the DB view.
- **Consistency model:** a write is one DB transaction updating `hfj_resource`, inserting `hfj_res_ver`, and rewriting the affected `hfj_spidx_*`/`hfj_res_link` rows. Reads of search results are therefore only as fresh as the index rewrite, which is synchronous here. Search-result *caching* is the real staleness source (HAPI caches identical searches for 60 s; the middleware opts out per call).
- **Write amplification:** every create fans out to N index rows. Fine at this scale; at EMR scale the index tables dominate storage and vacuum load. Plan partitioning (HAPI supports partitioning by tenant/compartment) and autovacuum tuning on the `spidx` tables before volume, not after.
- **Gaps:** superuser-ish single role shared by HAPI and the dashboards (read-only is enforced only by session settings and a SQL guard); no pooler (PgBouncer); `n_live_tup` used for row counts is an estimate; schema coupling to `hfj_*` names that change across HAPI majors; no encryption at rest; `hapi/hapi` credentials; no retention/expunge policy, so deleted resources keep their history.
- **Production path:** managed Postgres with PITR and cross-region backup; PgBouncer; a dedicated read-only role on a replica for analytics (and column-level grants that exclude PHI the dashboards do not need); partitioning plan; `$expunge`/retention policy agreed with compliance; KMS-backed encryption; migration discipline pinned to a HAPI version, with schema changes rehearsed on a restored snapshot.

---

## 2. HAPI FHIR server: the engine

### ELI5
HAPI is the clerk at the front desk. You hand over a form in a very specific format; the clerk checks it is shaped right, files it, and remembers every earlier version of it. You can ask, "show me all the weights for this baby," and the clerk finds them.
- *Why:* so every program talks to one clerk in one language, and no one has to know how the filing works.
- *Other clerks:* several, free and paid.
- *What's missing:* the clerk lets anyone walk up, doesn't check IDs, and doesn't check most forms unless asked.
- *To be real:* a guard at the door, a rule that every form is checked, and a second clerk for busy days.

### Newcomer
- **What:** `hapiproject/hapi:v8.12.0-2`, FHIR **R4**, REST at `http://localhost:8080/fhir`. It speaks the standard interactions: create (`POST`), read, `vread`, update (`PUT`), delete, search (`GET /Observation?subject=...`), history, `$everything`, `$validate`, transaction bundles, bulk `$export` (enabled).
- **Key ideas to learn:**
  - **Versioning and ETags:** every update bumps `meta.versionId`; send `If-Match: W/"1"` so a stale edit fails instead of overwriting. HAPI answers **409** (the spec says 412).
  - **Conditional create:** `If-None-Exist: identifier=sys|val` makes a create safe to retry (201 the first time, 200 after).
  - **Transactions:** a `Bundle` of type `transaction` is all-or-nothing.
  - **Referential integrity:** HAPI refuses to delete a resource that others reference (409). That is why test cleanup deletes in dependency order.
- **Configuration here:** write-time validation is **off** (`requests_enabled: false`); you call `$validate` explicitly. `store_meta_source_information: SOURCE_URI_AND_REQUEST_ID` makes HAPI persist the caller's `X-Request-ID` on each version.
- **Alternatives:** Medplum, Aidbox, Firely Server, Smile CDR (the commercial HAPI), IBM FHIR / LinuxForHealth, or a cloud service (Google Healthcare API, AWS HealthLake, Azure Health Data Services).
- **Gaps:** no authentication or authorization (anyone on the network can read and write); no terminology server loaded, so code systems like LOINC/SNOMED are not truly validated (a wrong code still passes); no Indian profiles (ABDM/NDHM IGs); single instance; first start took about 195 s and the first `$validate` took 24 s once.
- **To production:** put it behind an API gateway with OAuth2/SMART on FHIR; turn validation on with the right implementation guides; load a terminology service; run several stateless replicas against one database.

### Veteran
- **Decision and fit:** HAPI JPA starter is the reference implementation: widest R4 conformance, most operations, biggest community, and an interceptor model that is where every production concern (authz, audit, consent, tenancy, validation, request-ID propagation) eventually attaches. Trade-off: it is a heavy JVM app with a slow cold start and a schema you do not own.
- **Mechanics worth knowing:**
  - Optimistic concurrency via `If-Match`; HAPI returns 409 where the FHIR spec says 412. Handle both at every call site.
  - Concurrent conditional creates are guarded by a search-URL mechanism (visible as `search_url_present` on `hfj_resource`); still, idempotency should be designed on a business identifier, as the middleware does (`urn:h2f:idempotency|<key>/<role>` + `ifNoneExist`).
  - Search cache: identical searches within 60 s return cached results unless `Cache-Control: no-cache`. State checks that feed validation must bypass it; the middleware does.
  - `$validate` cost is dominated by profile/package loading on first use and by terminology expansion; one cold call took 24 s here. In a hot path, validate in a warmed worker pool or asynchronously, never inline on the first request after deploy.
  - Referential integrity on delete is enforced; `_cascade=delete` did not work in this config (observed), so deletes must go in dependency order.
- **Alternatives, honestly:** Medplum (Postgres, TypeScript, good DX, hosted option; less operation coverage); Aidbox (Postgres JSONB, fast, commercial); Firely (.NET, strong validator); Smile CDR (HAPI plus enterprise: MDM, subscriptions, SMART, clustering). Managed clouds trade control for operations and lock-in; verify the operations you rely on (`$everything`, `_include`, conditional ops, history) are all supported.
- **Gaps:** open endpoint; no `AuthorizationInterceptor`/SMART scopes; no `AuditEvent` (the middleware writes `Provenance`, which is a different thing); no consent enforcement; no terminology server (validity ≠ correctness: local codes pass); write-time validation off; single node with in-JVM caches; no tuning (heap, connection pool, `reuse_cached_search_results_millis`); Lucene/Hibernate Search for `_text`/`_content` not configured for scale; R4 only.
- **Production path:** gateway + OAuth2/OIDC (Keycloak/Okta) with SMART-on-FHIR scopes mapped to a HAPI `AuthorizationInterceptor`; `AuditEvent` generation (and ship to a WORM store); consent via `Consent` + a policy interceptor; load India's ABDM FHIR profiles through `implementationguides`, enable request validation on a profile-by-profile basis; terminology via Snowstorm/Ontoserver or HAPI's own loader (LOINC, SNOMED CT, UCUM); Elasticsearch/OpenSearch for full-text; multiple replicas behind a load balancer with the search cache disabled or externalized; readiness probes that call `/fhir/metadata`; resource limits and JVM tuning; Subscriptions into Kafka for downstream consumers; bulk export to object storage; blue/green upgrades rehearsed against a restored snapshot because HAPI majors can change schema.

---

## 3. FhirClient: the one way to call HAPI (`core/client`)

### ELI5
It is the phone we always use to call the clerk. Instead of every person dialing differently, everyone uses this phone. It also keeps a little notebook of every call it made and how long each took.
- *Why:* so there is one place to fix problems.
- *Other phones:* there are fancier ones.
- *What's missing:* if the line is busy, it just gives up rather than trying again.
- *To be real:* it should call back, wait politely, and show its ID.

### Newcomer
- **What:** `FhirClient` (about 200 lines over `httpx`). It exposes capabilities, create (with `If-None-Exist`), read, vread, update (with `If-Match`), delete, search (follows `next` links), history, `$everything`, `$validate`, transactions, plus small helpers (`create_patient`, `add_observation`).
- **How:** every call returns a `Response` with `status`, `resource`, `location`, `etag`; errors raise `FhirError` carrying HAPI's `OperationOutcome`. Each call is timed and reported to an optional `on_call` hook (`method, url, status, ms`), which is how the middleware builds its request log and the UI's trace panel. Default headers (like `X-Request-ID`) go on every call.
- **Try it:** `make demo` runs a narrated tour; `uv run python -m fhirlab.core.client --help`.
- **Alternatives:** `fhirpy`, `fhir.resources` (typed models), `fhirclient` (SMART), or plain `requests`.
- **Gaps:** synchronous only; no retry/backoff, circuit breaker or per-call timeouts (one global 60 s); no auth header support; no PATCH; no bulk-export helper; search results are fully materialized in memory.
- **To production:** retries with jittered exponential backoff on idempotent calls only, timeouts per operation, connection-pool limits, an auth hook (bearer token refresh), and optionally an async variant.

### Veteran
- **Decision:** a deliberately thin client that hides nothing about HTTP (status, `Location`, `ETag` always visible) because the project's goal is to expose FHIR semantics, and because every higher layer (middleware, HL7v2 validator, tests) needed the same behaviour. Convergence matters more than cleverness: there was a second hand-rolled `$validate` call in the HL7v2 package; it was folded into this client.
- **Design notes:** the `on_call` hook is the seam for observability without the client importing `obs` (the dependency direction stays core-internal). Timing is `perf_counter` around the transport call only. 4xx/5xx → `FhirError`; `validate()` converts a 422 back into the issue list, because for validation a 422 *is* the answer.
- **Gaps:** no retry taxonomy (only GET/PUT-with-If-Match/conditional create are safe to retry; POST create without `If-None-Exist` is not); no hedging; no deadline propagation; connection reuse depends on a long-lived `httpx.Client` (the UI path constructs short-lived ones in places); unbounded memory on large searches; no W3C `traceparent` propagation (only `X-Request-ID`, kept to 16 alphanumerics because `hfj_res_ver.request_id` is a 16-character column).
- **Production path:** idempotency-aware retry policy; per-operation deadlines derived from the caller's budget; token provider interface with refresh and 401 replay; `traceparent` plus request ID; streaming/paginated iterators; optional async twin sharing the same request-building code; contract tests against a recorded HAPI to catch version drift.

---

## 4. NICU middleware: the rules in front of the engine (`clients/nicu`)

### ELI5
Before a nurse's form reaches the clerk, a careful helper reads it. Is this nurse allowed to do this? Is anything blank that shouldn't be? Does a 3.9 kg baby born at 27 weeks make sense? Is the baby really still in the hospital? Only if everything looks right does the helper pass it on, all in one envelope: either every page is filed, or none.
- *Why:* the clerk will file almost anything that is shaped right, so we need a helper who also checks that it makes sense.
- *Other helpers:* a hospital could buy an off-the-shelf one.
- *What's missing:* the helper's ID check is pretend, and nobody keeps a log of who looked at what.
- *To be real:* real ID cards, a written record of every action, and rules reviewed by doctors.

### Newcomer
- **What:** a FastAPI service on `:8000`. Endpoints: `POST /login`, `GET /catalog`, `GET /practitioners`, `GET /patients`, `POST /validate` (dry run, writes nothing), `POST /submit`.
- **The pipeline (`pipeline.py`):** authz → field → clinical → state → map → HAPI `$validate` → plan. Stages are kept separate so each message says *what kind* of problem it is:
  - **authz:** a table of operation × role (`catalog/*.yaml`). Login checks the practitioner's `PractitionerRole` *in HAPI*, so picking "Doctor" in the UI for a nurse is refused (403).
  - **field:** type, required, range, options, pattern, conditional fields (`catalog.py`). Required-ness is also pushed to the UI so `*` markers match the server.
  - **clinical:** values contradict each other or common sense (`checks.py`).
  - **state:** does it fit what HAPI already holds (active admission, duplicate mother/phone/DOB, last weight, consent)? Lookups bypass HAPI's search cache.
  - **map:** `mapping.py` turns form values into one transaction `Bundle`.
- **Safety patterns worth learning:**
  - **Idempotency:** every created resource carries `urn:h2f:idempotency|<key>/<role>` and uses a conditional create, so retrying a submit creates nothing twice (201 then 200).
  - **Validate-then-submit:** `/submit` requires the payload hash from `/validate`, re-runs the whole pipeline (the world may have changed), and refuses if the payload differs (409).
  - **Optimistic locking:** updates carry `If-Match`.
  - **Provenance:** each write records who did it.
- **Auth:** an HS256 JWT built with the standard library (`auth.py`), 1 h TTL, signed with `SIM_SECRET` (a dev default). It is structurally real but not secure.
- **Alternatives:** HAPI interceptors (put rules inside the server), a commercial integration engine (Mirth, Rhapsody), or a BFF/GraphQL layer.
- **Gaps:** dev secret and no scopes; no `AuditEvent`; no rate limiting; several sequential HAPI searches per request (measured: about 97% of request time is spent waiting on HAPI, p95 around 1.4 s); heuristics are not clinical guidance; LOINC subset only; ABHA is simulated; IST timezone hard-coded; single hospital.
- **To production:** real identity provider; per-operation scopes; parallelize or cache state lookups; audit trail; clinician-reviewed rules under version control; load testing.

### Veteran
- **Decision:** the middleware is a policy enforcement and orchestration tier, not a pass-through. It exists because (a) HAPI deliberately validates structure, not clinical coherence or workflow state, and (b) write paths need atomicity, idempotency and authorization that depend on server state. Putting this in HAPI interceptors would couple it to the JVM and a HAPI major; keeping it outside makes it replaceable and testable (`FakeState`, no network).
- **Design notes:**
  - *Spec as data:* one YAML per operation drives the form, the permission check and the field validation; mapping stays code because it needs logic. One source of truth, shipped to the UI over `/catalog`, including `unit_policy`, so the client cannot drift.
  - *TOCTOU:* `/validate` is advisory; `/submit` revalidates against fresh state and binds to a payload hash that includes actor and event time. Residual race (two writers between revalidate and commit) is closed by `If-Match` on updates and conditional creates on inserts, inside one transaction bundle.
  - *Idempotency scope:* keyed per submission UUID and role within the bundle; retry returns the same `Location`s. Different payload, same key → 409. There is no TTL or key registry, so a client reusing a key years later gets old results; a real system bounds this.
  - *Attribution:* `Provenance` for authorship; this is not an audit log (no reads, no denials).
  - *Observability hooks:* `annotate(op, outcome, actor, role)` plus `record_upstream` feed the request log without the handlers knowing about log format.
- **Known gaps (specific):** HS256 with a default secret and no key rotation; no `aud`/`iss`/scope claims; roles come from `PractitionerRole.code` with a free-text-ish mapping; no per-patient access control (any nurse can see any baby); `GET /patients` and friends are unauthenticated (the UI calls `/catalog`, `/practitioners`, `/patients` on every Streamlit rerun, which made them the top three routes in the log by volume); sync endpoints run in the threadpool, so a slow HAPI call holds a worker; no retries or circuit breaker toward HAPI; no pagination on state searches (`_count=100`); clinical thresholds are placeholders; duplicate detection is warning-only and keyed on phone+DOB (the twin problem is real); no schema/versioning for the catalog (changing a form mid-flight changes validation for in-progress sessions).
- **Production path:** OIDC with scopes (`patient/Observation.write` style) and `SMART` launch if embedded in an EMR; ABAC for ward/unit scoping; `AuditEvent` for reads, writes and denials to an immutable store; pooled async HAPI client with bounded concurrency and a cache for reference data (practitioners, catalog) with explicit invalidation; request budgets and breakers; the catalog versioned and signed, with the version recorded in `Provenance`; clinician-owned rule review and a regression corpus of "must warn / must block" cases; ABDM integration (ABHA verification, HIP/HIU callbacks) behind an adapter; canary deploys with the existing request log as the SLI source.

---

## 5. Observability: request log, analytics, probes (`core/obs`)

### ELI5
Imagine a guest book at the front desk. Each time someone comes in, the helper writes one line: what they wanted, how long it took, whether it went well. Later we read the book and spot slow days and angry customers. We never write down anything private, like names.
- *Why:* so when something is slow or broken, we can see where.
- *Other guest books:* there are big professional ones.
- *What's missing:* it only notes people who came through the main door; those who sneak in a side door aren't recorded. The book never gets a new volume.
- *To be real:* write in a shared, bigger book, and ring a bell when things go wrong.

### Newcomer
- **What:** an ASGI middleware that writes **one JSON line per request** to `data/logs/middleware.jsonl`: time, request id, method, **route template** (`/patients/{pid}`, not the raw path), status, duration, cause category, actor and role, business operation, outcome, and every HAPI call it made (type, interaction, status, ms, start offset).
- **Ideas to learn:**
  - **RED metrics** (Rate, Errors, Duration) and **percentiles** (p50/p95/p99; averages hide the slow tail).
  - **Categorize by cause**, not just status code: `conflict` (409) and `validation` (422) are both 4xx but need opposite reactions.
  - **Correlation id:** the same `X-Request-ID` is forwarded to HAPI, which stores it in `hfj_res_ver.request_id`, so a database row links back to the log line that created it.
  - **No PHI in logs:** no bodies, no query values, no ids.
- **Pieces:** `record.py` (the contract), `middleware.py`, `store.py` (a `LogStore` port; JSONL adapter), `analytics.py` (pure pandas view models: KPIs, time series, per-route distribution, histograms, waterfall), `probe.py` (active latency probes), `api.py` (optional `/ops/*` JSON).
- **Alternatives:** OpenTelemetry + Prometheus/Grafana (metrics), Loki or Elasticsearch (logs), Jaeger/Tempo (traces), Datadog/New Relic.
- **Gaps:** one file, no rotation; only requests through the middleware are recorded (scripts that call HAPI directly are invisible); no traces, no metrics endpoint, no alerts or SLOs; each dashboard refresh re-reads the file tail.
- **To production:** ship logs with Vector/Promtail to Loki; emit OpenTelemetry traces and Prometheus metrics; define SLOs and alerts.

### Veteran
- **Decision:** a pure-ASGI middleware (no `BaseHTTPMiddleware`: no body buffering, no extra task, contextvar visible to worker threads) emitting a typed record behind a `Sink`/`LogStore` port. The contract (`RequestLog`) is the only coupling between producer and every consumer, so the store can move to Loki/ClickHouse/SQLite and the UI to React with no change in between. Cardinality is bounded by construction (route templates, resource types, no ids).
- **Analytics choices:** percentiles over means; log-spaced histograms with shared edges across routes (so rows align); "self time vs upstream time" decomposition, which is the single most useful number here (it showed ~97% of time in HAPI); categories keyed on *cause* to match triage.
- **Cross-tier correlation:** the request ID is the join key between the log, HAPI (`hfj_res_ver.request_id`, enabled by `store_meta_source_information`) and the Database page. 16 alphanumerics because the `request_id` column is 16 characters wide.
- **Gaps:** `JsonlSink` takes an in-process lock; with multiple uvicorn workers you rely on `O_APPEND` atomicity for small writes, which is true on local POSIX filesystems but not a design to depend on; no rotation or retention; the reader scans the last N lines per refresh (O(N)); no sampling; no W3C trace context, so no cross-service spans; no RED metrics export; probes are manual; synchronous log write on the response path (the `finally` block) adds latency under disk pressure; coverage gap for non-middleware callers (HL7v2 batch, ad-hoc scripts, `curl`).
- **Production path:** OpenTelemetry SDK for traces/metrics/logs with OTLP to a collector; keep the JSONL contract as the structured-log schema (it maps cleanly to OTel log attributes); Prometheus histograms for latency with exemplars linking to traces; SLOs (availability, p95 for `/validate` and `/submit`) with multi-window burn-rate alerts; async/batched sink with backpressure and a bounded queue (drop-with-counter rather than block); retention and legal-hold policy; a periodic synthetic probe from outside the network; an `AuditEvent` stream kept separate from operational logs because the retention and access rules differ.

---

## 6. Database view and Adminer (`core/db`)

### ELI5
It's a window into the filing cabinet. You can look at the drawers and see how one baby's papers piled up over time, but the window is glass: you can look, not touch.
- *Why:* so a person can see what really got stored and when.
- *Other windows:* there are fancier tools made just for this.
- *What's missing:* the window is made of the same glass as the door, so a clever person might open it.
- *To be real:* give the window its own separate, locked room with only the parts people should see.

### Newcomer
- **What:** `PgRepo` runs read-only queries; a pure `service` turns rows into meaning. The Database page shows three views: a **patient timeline** (every stored version of everything related to a patient, with field-level diffs), a **landscape** (resource mix, write activity, who-references-whom), and **tables & SQL** (browser plus a guarded SQL box).
- **How a timeline is built:** start at the patient's `res_id`; collect resources that link to them via `hfj_res_link`, plus `Provenance` about those; read every row from `hfj_res_ver`, oldest first; summarize each JSON in one line; diff v(n) vs v(n-1).
- **Safety:** the session is `default_transaction_read_only=on` with a 5 s statement timeout; the SQL box accepts a single `SELECT/WITH/EXPLAIN` and rejects write/DDL keywords; table names are checked against the real list before being quoted.
- **Adminer** (`:8081`) is a stock web UI for the same database, handy for poking at tables.
- **Alternatives:** pgAdmin, DBeaver, Metabase/Superset/Grafana on a replica, or a FHIR-level view using only REST.
- **Gaps:** same DB role as HAPI; a keyword filter is defense in depth, not a sandbox; coupled to HAPI's table names; PHI is visible to anyone who opens the page; no authentication on the console.
- **To production:** a dedicated read-only role on a replica with column grants, row-level policies, audit of every query, and SSO in front of the UI.

### Veteran
- **Decision:** a `DbRepo` port (`Protocol`) with a Postgres adapter, so the service and every UI depend on DataFrames with documented columns, not SQL. The repo is the only code that knows `hfj_*`, which isolates HAPI-version drift to one file. The same service is exposed as JSON (`core/db/api.py`) for non-Streamlit front ends.
- **Design notes:** compartment resolution is two-hop (direct references to the patient, then `Provenance` pointing at those) because FHIR's Patient compartment includes `Provenance.target`; encodings other than inline JSON (`JSONC`, LOB) are not handled, which is fine for this config and wrong in general; delete markers (`res_encoding = 'DEL'`) are surfaced as events, not hidden.
- **Gaps:** read-only is enforced by session GUC and a regex guard, both bypassable by a privileged role, so the role itself must be read-only; the regex rejects some legitimate queries and is not a parser; row counts come from `pg_stat_user_tables` estimates; no query cost guard beyond timeout; no result-size limit server-side (`fetchmany` after execution); timestamps are naive UTC, converted only at display; compartment logic ignores HAPI partitioning; large histories would need pagination and server-side diffing.
- **Production path:** replica + least-privilege role (no access to free-text clinical fields unless justified), query audit via `pgaudit`, connection through a pooler with per-user attribution, `EXPLAIN`-based cost cap or a curated query catalog instead of free SQL, break-glass access workflow for PHI, tokenization or masking for demo/analytics use, and a FHIR-native alternative (Bulk `$export` → warehouse) for anything heavy.

---

## 7. Console: the Streamlit frontend (`console`, `app/console.py`)

### ELI5
It's the dashboard in front of the helper, with three screens: one to fill the nurse's form, one that shows how busy and healthy the helper is, one that shows what's in the cabinet. It only shows things; it doesn't decide anything.
- *Why:* so people can see what is happening without reading computer text.
- *Other dashboards:* many.
- *What's missing:* anyone who finds it can open it, and it asks the helper the same questions over and over.
- *To be real:* a login, and remember answers instead of re-asking.

### Newcomer
- **What:** Streamlit multipage app. Pages are `Client` (the form simulator), `Server` (request health), `Database` (HAPI's tables). `app/console.py` only wires adapters to pages and owns navigation.
- **Key ideas:** *Streamlit reruns the whole script on every interaction*, so state lives in `st.session_state`. Pages receive their data source as an argument (`server_page(store, …)`), so they can be tested with `AppTest` and fakes. Charts are Altair (Vega-Lite JSON), built in `console/charts.py` from DataFrames.
- **Client page:** filters (role, practitioner, operation, baby) in the main page; the form on the left; validation and FHIR results pinned on the right so you see them after pressing Validate at the bottom of a long form. **Validate** must pass before **Submit** enables, and editing the form afterwards disables it again.
- **Server page:** *Pulse* (health, trends, per-route latency distribution), *Requests* (filter, click a row → waterfall of its HAPI calls), *Dependencies* (own time vs HAPI time, probes).
- **Alternatives:** React/Next.js (the charts and the JSON APIs are already portable), Grafana for the Server page, Retool/Appsmith for internal tools.
- **Gaps:** no login; each rerun calls the middleware several times (chatty); no caching strategy; Streamlit is not meant for many concurrent users; styling constrained by the framework; the stale-module quirk (imported modules are cached in the running process, so restart after editing chart code).
- **To production:** a React front end over the existing JSON APIs, SSO, and server-side caching.

### Veteran
- **Decision:** Streamlit for speed of iteration, with every non-rendering concern pushed down a layer so the UI is disposable: view models in `obs.analytics`/`db.service`, charts as Vega-Lite specs, ports for the data sources, optional JSON routers (`/ops/*`, `core.db.api`) that a React app can call. The page-per-concern split plus `test_architecture.py` keeps pages from importing each other.
- **Design notes:** the Client page is deliberately a thin client of the middleware (it never calls HAPI); the "Trace" tab shows both hops and the `request_id` that finds the call on the Server page. Chart conventions (fixed pixel row heights, no dual axes, fixed categorical colour order, direct labels) are codified in `console/charts.py`.
- **Gaps:** the rerun model drives chatty I/O (the catalog, practitioner and patient endpoints are the top three routes by volume, almost entirely from UI reruns); no `st.cache_data` on reference data; session state is per-process memory (no sticky sessions or shared store for scale-out); no authn/z; limited accessibility testing (colour validation was applied to the palette, but there is no screen-reader pass); Streamlit's file watcher does not reliably reload imported package modules, so dev loops need a restart.
- **Production path:** if it remains an internal tool: SSO via a reverse proxy (oauth2-proxy), TTL caches for reference data with explicit invalidation, per-user rate limits, and a deployment with a few replicas behind sticky sessions. If it becomes a product UI: React + a typed API client generated from the middleware's OpenAPI, charts rendered from the same Vega-Lite specs, and the Server page replaced by Grafana over the OTel stack.

---

## 8. HL7v2 batch client (`clients/hl7v2`)

### ELI5
Old hospital computers talk in a secret code made of pipes and letters, and sometimes they scribble. This is a translator that turns the code into the new clear language. To train and test it, we first make lots of pretend messages, then smudge some of them on purpose, and see how many it still translates right.
- *Why:* because many hospitals still speak the old code.
- *Other translators:* there are professional ones.
- *What's missing:* we have only practiced on pretend messages, and the part that can handle the really smudged ones (the clever part) isn't built yet.
- *To be real:* test with real, permission-granted messages, and add the clever helper for the hard cases.

### Newcomer
- **What:** a *batch* client of the core: it processes many messages at a time, including deliberately broken ones, and scores itself against known-correct answers. It is a subset of "client" work, so it lives under `clients/` beside the NICU client and depends only on `core`.
- **The pipeline:** Synthea generates 500 synthetic patients → `facts` (what HL7v2 can carry) → clean HL7 v2.5.1 messages (`ADT^A01`, `ADT^A08`, `ORU^R01`) **and** a *golden* FHIR bundle (the answer key) → an **injector** corrupts messages at 5 levels (0 to 0.8) with 8 kinds of damage (misplaced Z-segments, local lab codes, free text in coded fields, missing components, mangled units, mixed date formats, truncated names, bad encoding) → a **tolerant parser** (never raises) → a **YAML rules mapper** (maps what the standard says; records typed `FieldError`s for the rest) → HAPI `$validate` → **score** field by field vs golden.
- **Neat trick:** corruption is *nested* (what breaks at 0.4 also breaks at 0.6), so accuracy curves show difficulty, not luck.
- **Result so far (rules only):** critical-field F1 1.00 on clean messages, 0.95 at 0.2, 0.79 at 0.8; the share that would need an agent climbs from 0% to nearly 100%.
- **Concepts to learn:** MLLP framing, segments/fields/components, Z-segments (site-local extensions), LOINC vs local codes, UCUM units, "valid is not correct" (HAPI will accept a wrong LOINC).
- **Alternatives:** Mirth Connect / NextGen, Rhapsody, InterSystems IRIS, Apache Camel, HAPI-HL7v2 library, Microsoft FHIR-Converter (Liquid templates), Google Whistle, LinuxForHealth hl7v2-fhir-converter, `python-hl7`/`hl7apy`.
- **Gaps:** synthetic data and synthetic damage only; three message types; no transport (files, not MLLP sockets; no ACK/NACK); does not load results into HAPI (validate only); agents not built (`agents/` is a parked placeholder).
- **To production:** real, consented messages; MLLP/queue ingestion with ACKs and a dead-letter path; write to HAPI; learn mapping rules from reviewed fixes.

### Veteran
- **Decision:** the thesis is "deterministic rules for the standard path, agents only for the malformed tail", and the harness exists to *measure* that claim honestly: a golden bundle that is the projection of the source onto what v2 can carry (so recall has no unexplained ceiling), nested seeded corruption (monotone difficulty), reference-resolving scoring (compare `Type|business-key`, not random UUIDs), and a baseline with cost and escalation columns so an agent can be judged on cost per correct message, not accuracy alone. LiteLLM (provider-agnostic) was chosen for the later agent layer; own YAML mapper chosen over LinuxForHealth for control over failure typing.
- **Contract:** the mapper either maps exactly per spec or records a typed failure (`Z_SEGMENT, LOCAL_CODE, FREETEXT_CODED, MISSING_REQUIRED, UNIT, DATE, ENCODING, TYPE_MISMATCH, UNKNOWN`) and emits the most honest *valid* representation (local coding, `CodeableConcept.text`, unit preserved). `FailureKind` is the intended triage output type for the agent router.
- **Gaps:** corruption distribution is invented, not sampled from real interface logs, so absolute numbers do not transfer (relative ordering and the escalation curve do); single seed/split; HAPI validation passes local codes and wrong LOINCs (validity ≠ correctness), so correctness is only as good as golden; no terminology service; the disk cache on `$validate` keys on content only (stale if HAPI/IG changes); no dedup/idempotent ingest; no backpressure, batching, or partial-failure semantics because nothing is ingested; the `.cache`, `data/` and `results/` artifacts are ignored by git while the annotation DB is tracked, which needs a policy; review UI shares no auth with anything.
- **Production path:** (1) transport: MLLP listener or Kafka/SQS ingestion with ACK semantics, per-message idempotency keyed on `MSH-10`, DLQ, replay; (2) map with the rules path first, escalate on `FailureKind` with a budgeted, schema-constrained agent (propose → deterministic validator → accept/escalate-to-human), logging every decision; (3) compile accepted agent fixes into new rules so cost decays over time; (4) write as transaction bundles with conditional creates, request-ID stamped, observed through the same request log (see the coverage gap in section 5); (5) terminology via a real server, local-code mapping tables owned by the site; (6) shadow mode against the existing interface engine, diff-based cutover; (7) privacy: BAA/consent, de-identification for any model evaluation, no PHI to third-party LLMs without a contract and a redaction layer.

---

## 9. Tooling, tests, and working agreements

### ELI5
Before handing the toy to anyone, we shake it to see if anything rattles. We also write down the house rules so everyone builds the same way, and we clean up after ourselves.
- *Why:* so changes don't quietly break things.
- *Missing:* nobody shakes it automatically when we're not looking.
- *To be real:* a robot that shakes it on every change.

### Newcomer
- **Tools:** `uv` (Python 3.12, locked deps), `make` targets for everything, Docker Compose for Postgres + HAPI + Adminer, `pytest`.
- **Tests (73):** unit tests with fakes (no network); API tests with `TestClient`; Streamlit `AppTest` for pages; **live** tests against the real HAPI, which skip themselves if it's down and now **clean up the patients they create**; `test_architecture.py`, which fails if a package imports across the boundary.
- **Habits** (`CLAUDE.md`): build a small slice, test it immediately, run the real thing at boundaries, commit in isolated chunks, keep one way to do each thing, never edit HAPI tables with SQL.
- **Gaps:** no CI; no linter/formatter/type-checker configured; live tests share the developer's HAPI; compose has dev credentials; the apps themselves are not containerized.
- **To production:** CI that runs the suite against an ephemeral HAPI+Postgres, lint/type checks, container images, and a staging environment.

### Veteran
- **Decision:** the test pyramid is inverted on purpose at the edges: pure analytics/service logic is tested with fakes and tiny DataFrames (fast, deterministic); the integration surface (HAPI semantics: 409 vs 412, search cache, request-ID persistence, referential integrity) is tested live because those behaviours are exactly what mocks get wrong. The architecture test turns a convention into a build failure.
- **Lessons already paid for:** live tests that write to a shared server pollute it (they left phantom "active" babies); the fix is a teardown fixture that deletes in dependency order, restricted to patient-owned types (shared reference data must never be touched). Piping `pytest` into `tail` hides the exit status. Streamlit caches imported modules in the running process.
- **Gaps:** no CI gate; live tests are not hermetic (should target a per-run HAPI container or a dedicated test tenant/partition); no mutation or property-based tests on the mapper/corruptor (prime candidates: round-trip `emit → parse → map` equals golden at level 0); no performance/regression budget on `/validate` and `/submit`; no dependency/license/vuln scanning; no secrets management; no pre-commit.
- **Production path:** CI with service containers (`hapiproject/hapi` + `postgres`), tests partitioned into fast/live/slow with a required fast gate; testcontainers for hermetic live tests; property tests on the v2 emitter/parser; contract tests pinned to the HAPI image tag; load tests with budgets tied to the SLOs from section 5; SBOM + `pip-audit`/Trivy; signed images, pinned digests, secrets from a vault; trunk-based flow with isolated commits and required reviews (the commit discipline is already in place).

---

## What "production" would take, in order

1. **Secure the front door:** gateway, OAuth2/OIDC with SMART scopes, per-resource authorization, `AuditEvent`, secrets in a vault, TLS everywhere.
2. **Make storage safe:** managed Postgres, backups with PITR, read replica for dashboards with a least-privilege role.
3. **Make correctness real:** terminology server, ABDM/NDHM profiles, validation on writes, clinician-reviewed rules under version control.
4. **See everything:** OpenTelemetry traces and metrics, logs shipped off-box, SLOs and alerts, and coverage for *every* caller of HAPI (the batch client and scripts included).
5. **Scale and recover:** stateless HAPI replicas, caching strategy, load tests, runbooks, blue/green upgrades rehearsed on restored snapshots.
6. **Only then, intelligence:** the agent layer and ML features (see [`ideas.md`](ideas.md)) on top of an engine you can trust, observe and audit.
