"""HL7v2 batch client: a subset of "client" work: bulk, batched messages, some of them deliberately malformed.

    corpus/    Synthea → clean v2 + golden FHIR → corruption injector (5 levels)    (generator + injector)
    parse/ rules/   tolerant v2 parser; YAML rules mapper for well-formed messages
    validate/  HAPI $validate over the mapped output
    pipeline.py   configurations; the future agent layer plugs in here (agents/ is a placeholder, not started)
    eval/ review/   scoring harness; expert annotation UI (entrypoint: app/hl7v2_review.py)
Talks to HAPI only for validation; imports `fhirlab.core` only.
"""
