"""Optional HTTP face of the DB view models (JSON), so a React/Grafana front end can reuse everything.

    uvicorn fhirlab.core.db.api:app --port 8001    ->   /patients  /patients/{pid}/timeline  /resources  /activity  /links  /tables
"""

from __future__ import annotations

import json

import pandas as pd
from fastapi import APIRouter, FastAPI, HTTPException

from fhirlab.core.db import service
from fhirlab.core.db.repo import DbRepo, PgRepo, UnsafeSql


def _records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso"))


def make_router(repo: DbRepo) -> APIRouter:
    r = APIRouter(tags=["db"])

    @r.get("/patients")
    def patients() -> list[dict]:
        return _records(repo.patients())

    @r.get("/patients/{pid}/timeline")
    def timeline(pid: int) -> list[dict]:
        return _records(service.patient_timeline(repo, pid))

    @r.get("/resources")
    def resources() -> list[dict]:
        return _records(repo.resource_counts())

    @r.get("/activity")
    def activity(freq: str = "hour") -> list[dict]:
        return _records(repo.activity(freq))

    @r.get("/links")
    def links() -> list[dict]:
        return _records(repo.link_matrix())

    @r.get("/tables")
    def tables() -> list[dict]:
        return _records(repo.tables())

    @r.get("/tables/{name}")
    def rows(name: str, limit: int = 100, offset: int = 0) -> list[dict]:
        try:
            return _records(repo.table_rows(name, limit, offset))
        except UnsafeSql as e:
            raise HTTPException(404, str(e)) from None

    return r


app = FastAPI(title="HAPI database view (read-only)")
app.include_router(make_router(PgRepo()))
