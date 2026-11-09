# HL7v2 batch client

One of two **clients** of the core (the other is the interactive [NICU simulator](../nicu.md)). It is the *batch* kind of client:
many messages at once, some of them deliberately broken, mapped to FHIR by deterministic rules, scored against a known-correct answer.
It depends on `fhirlab.core` only (`core.fhir` constants and `core.client` for HAPI `$validate`) and never on the NICU client or the console.

```
Synthea (500 patients) ─▶ facts ─▶ clean HL7v2 ─▶ corruption injector (levels 0 · .2 · .4 · .6 · .8) ─▶ messy HL7v2
                                └▶ golden FHIR Bundle (the answer key)                                        │
                                                                                tolerant parser ─▶ YAML rules mapper ─▶ HAPI $validate ─▶ Outcome
                                                                                                                              │
                                                 scoring vs golden (eval/) ─▶ results/*.parquet ─▶ expert review UI (review/, :8501)
```

| Part | Path | What it does |
|---|---|---|
| Generator | `corpus/{synthea_load,facts,emitter,golden}.py` | Synthea FHIR → canonical facts → clean HL7 v2.5.1 (`ADT^A01`, `ADT^A08`, `ORU^R01`) **and** the golden Bundle. Golden is the *projection* of Synthea onto what v2 can carry, so recall has no unexplained ceiling. |
| Injector | `corpus/corrupt.py` | Seeded, **nested** corruption: a message corrupted at 0.4 is corrupted the same way at 0.6 plus more, so accuracy-vs-level curves show difficulty, not sampling noise. Eight types: Z-segment move, local lab code, free text in coded field, dropped required component, mangled units, mixed date formats, truncated name, bad encoding. |
| Parser | `parse/parser.py` | Never raises. Structural problems come back as data for triage. |
| Rules mapper | `rules/mapper.py`, `rules/templates/*.yaml` | Maps what the standard says; anything it cannot map correctly becomes a typed `FieldError` (`FailureKind`: Z_SEGMENT, LOCAL_CODE, FREETEXT_CODED, MISSING_REQUIRED, UNIT, DATE, ENCODING, TYPE_MISMATCH, UNKNOWN) plus the most honest *valid* representation. |
| Validation | `validate/hapi.py` | HAPI `$validate` through `core.client`, content-hash cached on disk (`.cache/validate`). Only error/fatal count. **Valid is not correct**: a wrong LOINC or a local code still passes. |
| Pipeline | `pipeline.py` | Named configurations returning an `Outcome` (bundle, path `fast`/`agent`/`escalated`, reason). Today only `rules_only`. |
| Eval | `eval/{run,score,charts}.py` | Field-level score vs golden (references resolved to business keys, not random UUIDs), per level. |
| Review | `review/*`, `app/hl7v2_review.py` | Expert annotation UI. `annotations/annotations.sqlite` is the one artifact you cannot regenerate. |
| Agents | `agents/` | **Parked.** Empty placeholder where a mapper/repair/terminology/reconciler layer would plug into `pipeline.py`. |

## Run it
```bash
make synthea    # 500 patients, seed 42 (needs Java 17)
make corpus     # facts → clean v2 + golden → 5 corruption levels   (data/corpus/)
make baseline   # rules-only over dev+test, all levels              (results/rules_only.parquet)
make charts     # summary table + figures                           (docs/clients/hl7v2/)
make review     # expert annotation + trends UI on :8501
```
Nothing here writes to HAPI: it only asks HAPI to validate. (Loading mapped bundles into HAPI is an obvious next step; see `docs/ideas.md`.)

## Baseline (rules only, dev+test, 982 messages per level)
| Corruption | critical-field F1 | valid % | escalated % | wrong fields / msg |
|---|---|---|---|---|
| 0.0 | 1.000 | 100.0 | 0.0 | 0.0 |
| 0.2 | 0.947 | 89.1 | 66.6 | 1.8 |
| 0.4 | 0.895 | 77.7 | 90.1 | 3.6 |
| 0.6 | 0.842 | 65.3 | 98.5 | 5.2 |
| 0.8 | 0.788 | 53.9 | 99.9 | 6.9 |

Reading it: rules are perfect on clean messages and degrade steadily; "escalated" is the share that would go to an agent. That gap is the thesis (rules for the standard path, agents only for the malformed tail), and it is what the parked agent layer would be measured against. Full table: [`summary.md`](summary.md).
