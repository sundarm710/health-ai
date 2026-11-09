"""A deliberately thin FHIR REST client over httpx.

It hides nothing about HTTP: every call returns a `Response` carrying the status code,
`Location`, `ETag` and the parsed body, and failures raise `FhirError` with the server's
`OperationOutcome` attached. Pass `trace=True` to print each request/response line, which
is the quickest way to see the REST conversation a FHIR server is having with you.

FHIR REST interactions implemented (spec: https://hl7.org/fhir/R4/http.html):

    capabilities  GET    /metadata
    create        POST   /<Type>                 (+ If-None-Exist => conditional create)
    read          GET    /<Type>/<id>
    vread         GET    /<Type>/<id>/_history/<vid>
    update        PUT    /<Type>/<id>            (+ If-Match => optimistic locking)
    delete        DELETE /<Type>/<id>
    search        GET    /<Type>?param=value     (follows Bundle `next` links)
    history       GET    /<Type>/<id>/_history
    everything    GET    /Patient/<id>/$everything
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

import httpx

from fhirlab.core.fhir import LOINC, MRN_SYSTEM, OBS_CATEGORY, UCUM

BASE = os.environ.get("HAPI_BASE", "http://localhost:8080/fhir")
FHIR_JSON = "application/fhir+json"

# code -> (display, UCUM unit, observation category). Small on purpose; extend as you learn.
OBSERVATIONS: dict[str, tuple[str, str, str]] = {
    "29463-7": ("Body weight", "kg", "vital-signs"),
    "8302-2": ("Body height", "cm", "vital-signs"),
    "8867-4": ("Heart rate", "/min", "vital-signs"),
    "8310-5": ("Body temperature", "Cel", "vital-signs"),
    "2339-0": ("Glucose [Mass/volume] in Blood", "mg/dL", "laboratory"),
    "4548-4": ("Hemoglobin A1c/Hemoglobin.total in Blood", "%", "laboratory"),
}


class FhirError(Exception):
    """Non-2xx response. `.outcome` is the parsed OperationOutcome when the server sent one."""

    def __init__(self, status: int, outcome: dict | None, text: str = ""):
        self.status = status
        self.outcome = outcome or {}
        issues = [i.get("diagnostics") or i.get("details", {}).get("text", "") for i in self.outcome.get("issue", [])]
        self.detail = "; ".join(x for x in issues if x) or text[:300]
        super().__init__(f"HTTP {status}: {self.detail}")


@dataclass
class Response:
    status: int
    resource: dict = field(default_factory=dict)
    location: str | None = None  # where a created/updated resource lives (includes _history/<vid>)
    etag: str | None = None  # W/"<versionId>"; feed to If-Match for optimistic locking

    @property
    def id(self) -> str | None:
        return self.resource.get("id")

    @property
    def version(self) -> str | None:
        return self.resource.get("meta", {}).get("versionId")


class FhirClient:
    def __init__(
        self,
        base: str = BASE,
        *,
        trace: bool = False,
        client: httpx.Client | None = None,
        on_call: Callable[[dict], None] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.base = base.rstrip("/")
        self.trace = trace
        self.http = client or httpx.Client(timeout=60)
        self.on_call = on_call  # receives {method, url, status, ms} for every request (UI trace panel, request log)
        self.default_headers = headers or {}  # sent on every call, e.g. X-Request-ID so HAPI stamps our id on its rows

    # ------------------------------------------------------------------ plumbing
    def _send(self, method: str, url: str, *, headers: dict | None = None, json: Any = None, params=None) -> Response:
        h = {"Accept": FHIR_JSON}
        if json is not None:
            h["Content-Type"] = FHIR_JSON
        h.update(self.default_headers)
        h.update(headers or {})
        t0 = time.perf_counter()
        r = self.http.request(method, url, headers=h, json=json, params=params)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if self.trace:
            extra = "  ".join(f"{k}={r.headers[k]}" for k in ("Location", "ETag") if k in r.headers)
            print(f"  {method:<6} {r.request.url}  ->  {r.status_code}  {extra}".rstrip())
        body = r.json() if r.content and "json" in r.headers.get("content-type", "") else {}
        if self.on_call:
            self.on_call({"method": method, "url": str(r.request.url), "status": r.status_code, "ms": ms})
        if r.is_error:
            raise FhirError(r.status_code, body if body.get("resourceType") == "OperationOutcome" else None, r.text)
        return Response(r.status_code, body, r.headers.get("Location"), r.headers.get("ETag"))

    # ------------------------------------------------------------------ interactions
    def capabilities(self) -> dict:
        """CapabilityStatement: what this server supports (resources, search params, operations)."""
        return self._send("GET", f"{self.base}/metadata").resource

    def create(self, resource: dict, *, if_none_exist: str | None = None) -> Response:
        """POST. 201 Created on a new resource. With `if_none_exist` (a search query such as
        `identifier=sys|val`) the server first searches: 0 matches -> 201, 1 match -> 200 and
        nothing is created, >1 -> 412. That makes the call idempotent."""
        headers = {"Prefer": "return=representation"}
        if if_none_exist:
            headers["If-None-Exist"] = if_none_exist
        return self._send("POST", f"{self.base}/{resource['resourceType']}", headers=headers, json=resource)

    def read(self, rtype: str, rid: str) -> Response:
        return self._send("GET", f"{self.base}/{rtype}/{rid}")

    def vread(self, rtype: str, rid: str, version: str) -> Response:
        return self._send("GET", f"{self.base}/{rtype}/{rid}/_history/{version}")

    def update(self, resource: dict, *, if_match: str | None = None) -> Response:
        """PUT replaces the whole resource (not a patch) and bumps versionId. Passing the ETag you
        last read as `if_match` makes a concurrent writer get an error (HAPI: 409; spec: 412) instead of being overwritten."""
        headers = {"Prefer": "return=representation"}
        if if_match:
            headers["If-Match"] = if_match
        return self._send("PUT", f"{self.base}/{resource['resourceType']}/{resource['id']}", headers=headers, json=resource)

    def delete(self, rtype: str, rid: str) -> Response:
        return self._send("DELETE", f"{self.base}/{rtype}/{rid}")

    def search(self, rtype: str, params: dict | None = None, *, limit: int | None = None, headers: dict | None = None) -> list[dict]:
        """GET /<Type>?... returns a Bundle (type=searchset); we follow `link[rel=next]` until the
        pages run out (or `limit` resources are collected) and return the bare resources.
        `headers={"Cache-Control": "no-cache"}` bypasses HAPI's 60 s search-result cache."""
        out: list[dict] = []
        bundle = self._send("GET", f"{self.base}/{rtype}", params=params, headers=headers).resource
        while True:
            out += [e["resource"] for e in bundle.get("entry", []) if e.get("search", {}).get("mode", "match") == "match"]
            nxt = next((link["url"] for link in bundle.get("link", []) if link["relation"] == "next"), None)
            if not nxt or (limit and len(out) >= limit):
                return out[:limit] if limit else out
            bundle = self._send("GET", nxt, headers=headers).resource

    def history(self, rtype: str, rid: str) -> list[dict]:
        """Every stored version, newest first. Deleted versions show up with request.method=DELETE."""
        return self._send("GET", f"{self.base}/{rtype}/{rid}/_history").resource.get("entry", [])

    def transaction(self, bundle: dict) -> Response:
        """POST a Bundle of type=transaction to the base URL: all entries succeed or none do.
        The response is a transaction-response Bundle with one status/location per entry."""
        return self._send("POST", self.base, json=bundle)

    def validate(self, resource: dict) -> list[dict]:
        """POST /<Type>/$validate (no storage). Returns the OperationOutcome `issue` list.
        Structural/profile checks only; it knows nothing about clinical sense."""
        rt = resource.get("resourceType", "Bundle")
        try:
            return self._send("POST", f"{self.base}/{rt}/$validate", json=resource).resource.get("issue", [])
        except FhirError as e:  # HAPI answers 422 when the resource is invalid
            return e.outcome.get("issue", [{"severity": "error", "diagnostics": e.detail}])

    def everything(self, patient_id: str) -> list[dict]:
        """$everything is a server-side *operation* (verb-style endpoint, prefixed with $)
        returning the patient plus all resources that reference them."""
        bundle = self._send("GET", f"{self.base}/Patient/{patient_id}/$everything", params={"_count": 200}).resource
        return [e["resource"] for e in bundle.get("entry", [])]

    # ------------------------------------------------------------------ domain helpers
    def create_patient(
        self, family: str, given: str, birth_date: str, gender: str, mrn: str | None = None
    ) -> Response:
        """Create a Patient. With an `mrn` the create is conditional on that identifier, so
        re-running with the same MRN returns the existing patient (200) instead of a duplicate."""
        patient: dict = {
            "resourceType": "Patient",
            "name": [{"use": "official", "family": family, "given": [given]}],
            "gender": gender,
            "birthDate": birth_date,
        }
        cond = None
        if mrn:
            patient["identifier"] = [{"system": MRN_SYSTEM, "value": mrn}]
            cond = f"identifier={MRN_SYSTEM}|{mrn}"
        return self.create(patient, if_none_exist=cond)

    def add_observation(
        self, patient_id: str, loinc: str, value: float, *, effective: str | None = None
    ) -> Response:
        """Create a numeric Observation for a patient. `loinc` must be a key of OBSERVATIONS."""
        display, unit, category = OBSERVATIONS[loinc]
        obs: dict = {
            "resourceType": "Observation",
            "status": "final",
            "category": [{"coding": [{"system": OBS_CATEGORY, "code": category}]}],
            "code": {"coding": [{"system": LOINC, "code": loinc, "display": display}], "text": display},
            "subject": {"reference": f"Patient/{patient_id}"},  # a Reference: how resources link
            "valueQuantity": {"value": value, "unit": unit, "system": UCUM, "code": unit},
        }
        if effective:
            obs["effectiveDateTime"] = effective
        return self.create(obs)

    def patient_observations(self, patient_id: str, code: str | None = None) -> list[dict]:
        """Observations for a patient, newest first. `code` filters by LOINC (search param `code`)."""
        params = {"subject": f"Patient/{patient_id}", "_sort": "-date"}
        if code:
            params["code"] = f"{LOINC}|{code}"
        return self.search("Observation", params)
