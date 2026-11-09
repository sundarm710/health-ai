# Five directions for production

Where this could head, ordered by how directly it builds on what already exists. Each idea names the **buyer** (a product company, a hospital, or both), the **AI/ML role**, what **already exists** in this repo, what is **new**, and the **first 30 days**.
Prerequisite for all five: the hardening in [`tiered.md`](tiered.md#what-production-would-take-in-order) (auth, audit, backups, terminology). None of these should ship before the engine is trustworthy.

| # | Idea | Primary buyer | AI/ML role | Builds most on |
|---|---|---|---|---|
| 1 | Integration copilot: agents fix messy HL7v2 | Integration-engine / health-IT product companies; hospital IT | LLM repair agents behind deterministic gates | `clients/hl7v2` |
| 2 | NICU early-warning and trend intelligence | Hospitals (NICUs); device and EMR vendors | Time-series risk models, explainable alerts | `clients/nicu` + `core/db` |
| 3 | ABDM-ready HIE gateway with smart patient matching | Hospital chains, HIE operators, PHR apps | Probabilistic matching / entity resolution | `core` + `clients/nicu` checks |
| 4 | Clinical data observability ("Datadog for clinical data") | Hospital CMIO/quality teams; platform vendors | Anomaly and drift detection | `core/obs` + `core/db` + console |
| 5 | Ambient capture to structured record, with guardrails | Product companies; hospitals | Speech + LLM extraction, validated by rules | catalog + `checks` + `mapping` |

---

## 1. Integration copilot: agents that repair messy HL7v2 feeds

**The problem.** Every hospital feed is slightly broken: local lab codes, free text in coded fields, Z-segments, odd date formats. Integration teams spend weeks to months hand-mapping each interface, then babysit the exceptions.

**The idea.** A pipeline where deterministic rules handle everything the standard covers, and a **budgeted agent** handles only the malformed tail: *propose a fix → a deterministic validator accepts or rejects → anything uncertain goes to a human*. Every accepted fix is **compiled into a new rule**, so the agent's cost decays over time ("agent writes, rules run").

**Who buys it.** Integration-engine and health-IT product companies (as a feature or an add-on that cuts onboarding time per hospital); hospital IT teams and HIEs (fewer stuck messages, fewer interface analysts).

**AI/ML role.** LLM mapper/repair/terminology agents; a router keyed on `FailureKind`; embedding or fuzzy search for local-code to LOINC suggestions; confidence calibration; evaluation on cost per correct message, not accuracy alone.

**Already here.** The nested-corruption corpus and golden bundles (a ready-made benchmark), the tolerant parser, typed `FieldError`/`FailureKind`, the rules baseline with `escalated_pct` and `cost_per_msg` columns, the expert review UI, and the parked `agents/` slot in `pipeline.py`.

**New work.** The agent layer itself; a rule-learning loop that turns reviewed fixes into YAML; an ingest path that writes to HAPI and appears in the request log; MLLP/queue transport with ACKs and a dead-letter queue; shadow-mode comparison against a hospital's existing engine; privacy controls (redaction, no PHI to third-party models without a contract).

**First 30 days.** Build the mapper+repair agents behind `pipeline.py`'s config switch; run the existing eval at levels 0.2 to 0.8; publish the curve of accuracy vs cost vs escalation against the rules-only baseline (critical F1 0.947 → 0.788).

**Risks.** Hallucinated codes that pass validation (validity is not correctness: needs a terminology server); real-world corruption differs from the synthetic kind; liability for silent mis-mapping, so every auto-accepted fix needs an audit trail.

---

## 2. NICU early-warning and trend intelligence

**The problem.** Preterm infants deteriorate quickly (sepsis, necrotizing enterocolitis, apnea, growth faltering). Signals hide in slowly drifting values scattered across notes and observations.

**The idea.** Turn the stream of weights, vitals, procedures and lab results into **trend features and risk scores**, shown beside the chart with plain-language reasons ("weight fell 5% over 48 h while a central line has been in place 9 days"). Start with transparent rules and trends; add ML where it beats them on retrospective data.

**Who buys it.** NICUs and children's hospitals (clinical value, quality metrics); EMR and bedside-device vendors (a module to sell). Clinical decision support in India's tertiary and private NICUs is an open space.

**AI/ML role.** Time-series models on irregular clinical data (gradient-boosted trees first, sequence models later); calibration and subgroup analysis by gestational age; explanations per alert; drift monitoring. The LLM role is narrow: summarize *why* an alert fired, never decide.

**Already here.** An admission/anthropometry/procedure/consent workflow that writes real FHIR; clinical plausibility checks (`checks.py`) that already encode "weight vs birth weight" and "GA vs weight"; the patient timeline and version diffs in `core/db`; a read-only data path for feature extraction.

**New work.** A feature store derived from the FHIR compartment (read replica); retrospective cohort and labels (needs real, consented data); an alert service delivering through CDS Hooks or a notification channel; alert-fatigue controls; model registry and monitoring.

**First 30 days.** Define 3 target outcomes with neonatologists; build the trend features and a rules baseline from the timeline view; replay history to count would-have-fired alerts; only then try a model.

**Risks.** This is likely regulated software (CDSCO/FDA SaMD territory); false alarms harm trust; synthetic data cannot validate it; requires clinician ownership and a safety case.

---

## 3. ABDM-ready HIE gateway with smart patient matching

**The problem.** India's ABDM (ABHA IDs, health information provider/user roles, consent manager) expects hospitals to exchange records on FHIR with patient consent. Hospitals have fragmented MPIs, duplicate patients, and twins who share a phone and date of birth.

**The idea.** A gateway that sits on the FHIR core and makes a hospital ABDM-ready: ABHA verification, consent-driven record sharing, ABDM-profile validation, and a **master patient index** with explainable duplicate detection.

**Who buys it.** Hospital chains and diagnostics networks (compliance and interoperability); HIE operators and state programs; PHR and insurer apps that need consented access.

**AI/ML role.** Probabilistic record linkage / entity resolution (names, transliteration variants, phone, DOB, address) with calibrated match scores and a human review queue; learned blocking to keep it fast; fairness checks across naming conventions.

**Already here.** FHIR core with versioning, conditional creates and idempotency; `Consent` and ABHA operations in the NICU catalog (simulated); a first duplicate check keyed on mother's phone + DOB (`possible_duplicates`), which already surfaces the twin ambiguity; `Provenance` on writes.

**New work.** Real ABDM sandbox integration; consent-aware authorization (HAPI interceptor + `Consent`); `AuditEvent` stream; ABDM FHIR profiles loaded into validation; MPI service with merge/unmerge semantics and history (HAPI MDM or a service beside it); callback endpoints for HIP/HIU flows.

**First 30 days.** Stand up ABDM sandbox credentials; model the consent artefact flow against the existing `Consent` operation; build the match-scoring prototype on synthetic duplicates with injected typos and twins; measure precision/recall.

**Risks.** Regulatory change and sandbox-to-production gaps; wrong merges are clinically dangerous (favour false negatives plus review); consent semantics are subtle.

---

## 4. Clinical data observability: "Datadog for clinical data"

**The problem.** Engineers monitor servers; nobody monitors the *data*. Feeds silently stop, a unit changes from kg to g, a lab starts sending a new local code, a ward stops recording weights. These failures are found weeks later by a researcher.

**The idea.** Extend the console from "is the server healthy?" to "is the **data** healthy?": freshness (a feed went quiet), completeness (required fields missing), plausibility (values outside ranges), drift (distribution shifts), schema/code-system changes, and **lineage** from any record back to the request that wrote it. Sell dashboards and alerting to the people accountable for data quality.

**Who buys it.** Hospital CMIO, quality and informatics teams; analytics and research groups that depend on the data; platform and HIE vendors as an embedded product.

**AI/ML role.** Anomaly detection on write rates and value distributions (seasonal baselines, changepoint detection); drift detection on code usage; an LLM to summarize "what changed this week" and draft a ticket, with the underlying numbers shown.

**Already here.** The request log with cause categories and percentiles; `request_id` stamped into HAPI so any row traces to its request (the lineage key); the Database page's activity and who-references-whom views; pure analytics layers that already feed both Streamlit and JSON routers; read-only DB access with guard rails.

**New work.** Close the coverage gap so *every* caller of HAPI is logged (batch jobs, scripts, other apps); a rules engine for data-quality checks over a read replica; time-series store and alert routing (with SLOs); OpenTelemetry/Prometheus export; multi-tenant views; a proper React front end.

**First 30 days.** Add the "log every caller" hook; define five data-quality monitors (feed freshness, missing weight in 24 h, unit drift, new local code, orphaned references); run them on a schedule against the existing database and page on a breach.

**Risks.** Alert fatigue; PHI handling in a monitoring tool (keep it to shapes and counts); getting access to production feeds.

---

## 5. Ambient capture to structured record, with guardrails

**The problem.** Nurses and doctors spend a large share of the shift typing into forms. Free-text dictation is fast but unstructured, so it can't drive checks or analytics.

**The idea.** Capture speech (or a note), have a model **propose** the structured form values, show them pre-filled for one-tap confirmation, and push them through the *same* deterministic pipeline that already exists: authorize → field checks → clinical checks → state checks → FHIR mapping → `$validate`. **The model proposes; the rules dispose.** Nothing reaches HAPI that a hand-typed form couldn't.

**Who buys it.** Product companies (an ambient-documentation module for NICU and ward workflows, an area where large vendors are active but local-language and low-bandwidth support is thin); hospitals as pilot partners.

**AI/ML role.** Speech recognition (Indian-English and code-switched Hindi/regional speech); LLM extraction into the catalog's field schema (the YAML catalog is already a machine-readable target schema); confidence scores that decide which fields need explicit confirmation; evaluation against clinician-verified transcripts.

**Already here.** The operation catalog (fields, types, ranges, required-ness) as data, served over `/catalog`; the full validate-then-submit pipeline with a payload hash so what the clinician confirmed is exactly what is written; idempotency and `Provenance`; the trace and request log to see what happened.

**New work.** The capture client and ASR; the extraction layer with schema-constrained output; a confirmation UI with per-field confidence; provenance that records "AI-assisted, confirmed by X"; latency budgets for use at the bedside; a de-identification path for model improvement.

**First 30 days.** Take 50 anonymized, consented-style example dictations; build the extractor against two catalog operations (`anthro.record`, `patient.admit`); measure field-level accuracy and how often the deterministic checks catch an extraction error that the clinician would have missed.

**Risks.** Hallucinated values that look plausible (the checks reduce but do not eliminate this, so confirmation is mandatory); privacy of audio; clinician trust and workflow fit decide adoption more than model accuracy.

---

## How to choose

- **Fastest to a demo with real signal:** #1 (the benchmark and baseline already exist) or #4 (mostly extending what is built).
- **Biggest clinical value, highest bar:** #2 (needs real data and a safety case).
- **Strongest India-specific market pull:** #3 (ABDM compliance is a forcing function).
- **Best product-company story:** #1 or #5; each has a measurable, sellable improvement (onboarding time; documentation time).
- **A sensible sequence:** harden the core → #4 (see everything) → #1 (use that visibility to make integration cheaper) → #3 or #2 as the data and partnerships mature → #5 once the pipeline is trusted enough to put a model in front of it.
