"""fhirlab: a HAPI FHIR server (the CORE) and the things that use it.

    core/      the engine side: FHIR constants, the one REST client, request-log observability, read-only DB view.
               Imports nothing outside core. Everything else is optional and depends on it, never the reverse.
    clients/   programs that talk to the core. Each is independent; they must not import each other.
        nicu/    interactive NICU form simulator (middleware API: authorise · check · map · transact)
        hl7v2/   batch client: HL7v2 messages (incl. deliberately broken ones) → FHIR, with eval and expert review
    console/   Streamlit frontend over core + nicu (pages: Client, Server, Database).

The boundary is enforced by tests/test_architecture.py.
"""
