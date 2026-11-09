"""Optional HTTP face of the analytics: the same view models as JSON, for any non-Streamlit front end.

    app.include_router(make_router(JsonlLogStore()))   ->   GET /ops/kpis  /ops/timeseries  /ops/routes  /ops/logs
"""

from __future__ import annotations

import json

from fastapi import APIRouter

from h2f.obs import analytics as an
from h2f.obs.store import LogStore


def make_router(store: LogStore) -> APIRouter:
    r = APIRouter(prefix="/ops", tags=["ops"])

    def frame(limit: int):
        return an.to_frame(store.read(limit))

    def records(df):  # DataFrame -> JSON-safe list (timestamps as ISO strings)
        return json.loads(df.to_json(orient="records", date_format="iso"))

    @r.get("/logs")
    def logs(limit: int = 500) -> list[dict]:
        return [x.to_dict() for x in store.read(limit)][::-1]

    @r.get("/kpis")
    def kpis(limit: int = 5000) -> dict:
        return an.kpis(frame(limit))

    @r.get("/timeseries")
    def timeseries(freq: str = "1min", limit: int = 5000) -> list[dict]:
        return records(an.timeseries(frame(limit), freq))

    @r.get("/routes")
    def routes(limit: int = 5000) -> list[dict]:
        return records(an.by_route(frame(limit)))

    @r.get("/categories")
    def categories(limit: int = 5000) -> list[dict]:
        return records(an.by_category(frame(limit)))

    return r
