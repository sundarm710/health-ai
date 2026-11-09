# NICU FHIR simulation: form → middleware → HAPI

A learning simulator for how an EMR turns a clinician's form entry into FHIR calls.
**Synthetic data only.** The clinical rules are heuristics for learning, not clinical guidance.

```
 Streamlit UI  ──HTTP──▶  FastAPI middleware  ──HTTP──▶  HAPI FHIR  ──SQL──▶  Postgres
 (hop 1)                  authz · parse · check · map     (hop 2)              (hop 3: use Adminer)
```

## Run it (three terminals, after `make up`)
```bash
make sim-seed   # once: 1 hospital, 2 doctors, 2 nurses (idempotent)
make sim-api    # terminal 1: middleware on :8000   (interactive docs at /docs)
make sim        # terminal 2: UI on :8502
make db-ui      # optional: look at the rows in Adminer
```

## The loop to try
1. Sidebar: pick **Nurse** → *Kavya Nair*. Operation **Form 1 · Register & admit baby** → fill it → **Validate**.
2. Read the tabs: **Issues** (which stage said what), **Parsed** (what the middleware understood and computed),
   **FHIR request** (the exact transaction Bundle), **Trace** (hop 1 and hop 2).
3. Break things on purpose, then fix and re-validate. **Submit** only enables when the *current* form passed.

| Try this | What you see | Why |
|---|---|---|
| Birth weight 9 | field error | catalog range 0.3–7 |
| 27 weeks, 3.9 kg | clinical **warning** (does not block) | cross-field plausibility |
| Birth date after the admission time | clinical error | values contradict each other |
| Switch to **Doctor**, choose Form 1 | authz error + the spec flag | the table says Doctor can't `patient.admit` |
| Tick "mismatched role", pick a nurse as Doctor | sign-in refused (403) | the server checks `PractitionerRole`, not the dropdown |
| Weight 0.8 kg after 1.38 | state warnings | compared to history already in HAPI |
| Remove a line that was never inserted | state error | needs an in-situ insertion |
| Withdraw consent that doesn't exist | state error | consent is state, not a delete |
| Edit the form after a green validation | Submit greys out | the payload hash no longer matches |
| Submit twice (same idempotency key) | `201`s then `200`s | conditional create → no duplicates |
| Discharge, then record weight | "no active admission" | state check on the Encounter |

## What each form becomes
| Operation | Resources written (one transaction) |
|---|---|
| `patient.admit` | Patient (baby) · RelatedPerson (mother) · Encounter · Condition · Observation ×2 (birth weight, GA) · Provenance |
| `anthro.record` | Observation per measurement (weight / length / head circumference) · Provenance |
| `consent.record` | Record: Consent (POST). Update/Withdraw: Consent (PUT, `If-Match`) · Provenance |
| `abha.link` | RelatedPerson (PUT with the ABHA identifier, `If-Match`) · Provenance |
| `procedure.log` | Insertion: Procedure (`in-progress` = in situ) + bundle Observation. Removal: PUT closes the insertion + a removal Procedure · Provenance |
| `patient.discharge` | Encounter (PUT → `finished`, disposition) · weight Observation · Provenance (Patient too if Death) |

## Design decisions worth understanding
* **A baby has no identifier.** The middleware mints a Baby ID (`NICU-xxxxxxxx`) and every created resource carries
  an *idempotency identifier* built from a per-form key. That is what makes retries safe. Twins get different
  keys, so they never collide on mother + date of birth (a duplicate *warning* still fires).
* **Role comes from the signed token,** never from the form. `/login` checks the practitioner's `PractitionerRole`
  in HAPI first. (Production: an identity provider issues the token, with SMART scopes.)
* **Validate ≠ Submit.** `/submit` re-runs everything against *current* server state and refuses a payload whose
  hash differs from the validated one.
* **State lookups bypass HAPI's search cache** (`Cache-Control: no-cache`). HAPI caches identical searches for
  60 s, which made "is a line in situ?" return a stale answer right after an insertion. Found by running it.
* **HAPI `$validate` is the last stage,** and it caught real mapping bugs while this was built (`birthTime` belongs
  on `_birthDate`; Consent needs a `policyRule`; PUT entries need absolute `fullUrl`s). It checks structure, not sense.

## Assumptions and simplifications
* The pasted spec was truncated in places. Every field the repo had to fill is marked **ⓘ assumed default** in the UI
  and `assumed:` in `src/fhirlab/sim/catalog/*.yaml`. **Form 16 (discharge) had no fields at all**; its form is invented.
  Please review these with someone who knows neonatal practice.
* Roles are Doctor and Nurse; Nurse takes the In-Charge column. Doctor can't admit; Nurse can't discharge.
* Gestational age is stored as total days (UCUM `d`) under LOINC 18185-9. Mother's age and blood group are local
  extensions on RelatedPerson. "Ambiguous" sex maps to `gender: other` plus an extension holding the original.
* The idempotency key is per form session. Re-submitting *changed* values with a key that was already used returns the
  originally stored resources (that is what idempotency means); the UI issues a new key after every successful submit.
* Out of scope: beds, census, tasks, alerts, chat, dictation, handover (Form 17), HAI/MDRO/incident forms.
  Adding an operation = a YAML file in `catalog/` + a mapper in `mapping.py` + checks in `checks.py`.

## Where the code is
`src/fhirlab/sim/`: `catalog.py` (spec as data, field checks) · `checks.py` (clinical, state) · `mapping.py` (FHIR) ·
`pipeline.py` (stages, submit gate) · `auth.py` (tokens) · `state.py` (HAPI lookups) · `api.py` · `ui.py` · `seed.py`.
Tests: `tests/test_sim.py` (unit tests need nothing running; the live test needs `make up && make sim-seed`).
