"""Read-only window into HAPI's Postgres, UI-free.

    repo.py      DbRepo port + PgRepo adapter (the only code that knows SQL / psycopg / HAPI's table names)
    service.py   pure: raw rows -> patient timelines, one-line summaries, version diffs
    api.py       optional HTTP router exposing the same view models as JSON

Swap PgRepo for anything that returns the same DataFrames (a FHIR-REST-backed repo, a fixture) and neither
the service nor any UI changes. Nothing here imports Streamlit.
"""

from fhirlab.dbview.repo import DbRepo, PgRepo, UnsafeSql

__all__ = ["DbRepo", "PgRepo", "UnsafeSql"]
