"""CORE: the HAPI FHIR engine and its direct access layer. Depends on nothing else in fhirlab.

    fhir.py   FHIR system URIs / code constants
    client/   FhirClient: the one way to call HAPI (REST interactions, per-call timing, request ids)
    obs/      what the server did: request log, analytics, probes (UI-free)
    db/       read-only view of HAPI's Postgres: repo (SQL), service (timelines, diffs)
"""
