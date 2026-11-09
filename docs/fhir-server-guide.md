# FHIR server lab: HAPI + Postgres

```
 you ──HTTP/JSON──▶  HAPI FHIR server (:8080/fhir)  ──JDBC──▶  Postgres (:5432)
 curl / h2f.client     REST API, search, validation          stores resources + search indexes
```

HAPI *is* the FHIR server: it implements the FHIR REST API and persists to Postgres through
its JPA layer. `src/h2f/client/` is a small Python client; `make demo` runs all of it.

```bash
make up      # Postgres + HAPI, waits until ready (first start ~1 min)
make demo    # narrated tour; run it twice to see idempotency
make psql    # look at what HAPI wrote
make down    # stop, keep data      make reset   # stop and wipe data
```

## 1. Interactions, with curl first and Python second

Do each in curl so you see the raw protocol, then read the matching client method in
`src/h2f/client/fhir_client.py`. Add `--trace` to any CLI command to print the HTTP calls.

| Goal | HTTP | Client method |
|---|---|---|
| What can this server do? | `GET /fhir/metadata` | `capabilities()` |
| Create | `POST /Patient` | `create()` / `create_patient()` |
| Read | `GET /Patient/1000` | `read()` |
| Update (whole-resource replace) | `PUT /Patient/1000` | `update()` |
| Old version | `GET /Patient/1000/_history/1` | `vread()` |
| All versions | `GET /Patient/1000/_history` | `history()` |
| Search | `GET /Observation?subject=Patient/1000&code=http://loinc.org\|29463-7` | `search()` |
| Operation | `GET /Patient/1000/$everything` | `everything()` |

```bash
curl -s -X POST localhost:8080/fhir/Patient -H 'Content-Type: application/fhir+json' \
  -d '{"resourceType":"Patient","name":[{"family":"Iyer","given":["Meena"]}],"gender":"female","birthDate":"1975-02-03"}' -i
# 201 Created, Location: .../Patient/<id>/_history/1, ETag: W/"1"
```

Then from the CLI:

```bash
uv run python -m h2f.client --trace create-patient --family Iyer --given Meena --birth-date 1975-02-03 --gender female --mrn A100
uv run python -m h2f.client add-obs <id> 29463-7 58.2 --effective 2026-11-05T09:00:00+05:30
uv run python -m h2f.client summary <id>
uv run python -m h2f.client search Patient family=Iyer
uv run python -m h2f.client history Patient <id>
```

## 2. Concepts to notice (each is visible in the demo output)

- **Resources are small, linked documents.** A weight reading is its own `Observation` with
  `subject: {reference: "Patient/1000"}`; the patient does not contain its observations.
  Linking by `Reference` is why `$everything` and `_include` exist.
- **The server assigns ids.** POST to the type URL, never to an id. The new URL comes back in
  `Location`. (PUT to a chosen id is "update-as-create", disabled by default.)
- **Versioning is built in.** Every PUT makes a new `versionId`; old versions stay readable.
  `DELETE` is also a version (see `history`).
- **Idempotency with conditional create.** `If-None-Exist: identifier=sys|value` turns a POST
  into "create unless it exists": 201 the first time, 200 the second, no duplicate. This is
  the answer to "what if my client retries after a timeout?".
- **Optimistic locking with `ETag` / `If-Match`.** Read, edit, PUT with `If-Match`. If someone
  else wrote in between you get an error instead of a silent overwrite. The spec says 412;
  HAPI returns **409** (we found out by testing, see `tests/test_fhir_client.py`).
- **Errors are data.** Failures return an `OperationOutcome` resource; `FhirError.outcome`
  holds it. A malformed value (`gender: "banana"`) fails at parse time with **400**. A
  well-formed but invalid resource (say a required field missing) passes parsing and only fails
  `$validate` or, with validation enabled, a **422**.
- **Search is not SQL.** Only parameters the server has *indexed* work (`GET /metadata` lists
  them). Results are paged Bundles; `search()` follows `link[rel=next]`.
- **Content negotiation.** `Accept` / `Content-Type: application/fhir+json` (add `_format=xml`
  to the URL and try it).

## 3. Look inside Postgres (`make psql`)

HAPI stores each resource as JSON text plus separate index tables for search.

```sql
-- current state of every resource
select res_type, res_id, res_ver from hfj_resource order by res_id;
-- every version ever written (this is what _history reads)
select res_id, res_ver, res_updated, res_text_vc from hfj_res_ver order by res_id, res_ver;
-- the search index: why GET /Patient?identifier=... is fast
select res_id, sp_name, sp_system, sp_value from hfj_spidx_token;
select res_id, sp_name, sp_value from hfj_spidx_quantity;   -- value-quantity for Observations
```

Questions to answer by experiment: what happens to `hfj_resource` and `hfj_res_ver` on a
PUT? On a DELETE? Which index rows appear when you add an Observation?

## 4. Exercises

1. Run `make demo` twice. Which steps change the second time, and why?
2. Add a height (`8302-2`) and a glucose (`2339-0`) reading. Search `Observation?code=...` for
   each, then `Observation?subject=Patient/<id>&date=ge2026-11-01`.
3. Fetch `Observation?_include=Observation:subject&subject=Patient/<id>`; the Patient arrives in
   the same Bundle, flagged `search.mode = include`.
4. Make a stale write on purpose: read a Patient, update it via curl, then PUT the old
   copy with its old ETag in `If-Match`.
5. `POST /Observation/$validate` with body `{"resourceType":"Observation"}`: it reports errors
   (`status` and `code` are required), yet a plain `POST /Observation` with the same body returns
   201 today. Then flip `hapi.fhir.validation.requests_enabled` to `"true"` in
   `docker-compose.yml`, `docker compose up -d`, and POST it again (expect 422). Compare with
   `"birthDate":"1999-13-45"` on a Patient, which fails parsing with 400 either way.
6. Delete a Patient, `GET` it (410 Gone, not 404), then look at its `_history`.
7. Restart with `make down && make up`: the data persists (named volume). Then `make reset`: gone.
8. Extend `OBSERVATIONS` with a blood-pressure panel. It has two components (systolic and
   diastolic) so it needs `component[]` rather than a single `valueQuantity`.

## 5. Where this connects to the HL7v2 project

`h2f.validate.hapi` already calls this server's `$validate`. The rules mapper emits
transaction Bundles with conditional creates (`h2f.fhir.transaction`), which is exactly step 1's
`If-None-Exist` mechanism applied to many resources in one atomic POST to `/fhir`.
Posting one of those Bundles and watching the rows appear in Postgres is a good next experiment.

## Notes

- Image tags are pinned (`hapi:v8.12.0-2`, `postgres:17`) so behaviour does not shift under you.
- Credentials in `docker-compose.yml` are local-dev only. HAPI has no authentication here;
  do not expose port 8080 beyond localhost, and never load real patient data into it.
