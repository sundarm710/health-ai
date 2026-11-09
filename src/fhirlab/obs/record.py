"""The log contract. Every consumer (store, analytics, UI, API) speaks only this shape.

PHI rule: a record never contains request/response bodies, query values or resource ids from URLs.
It carries *shapes* (route template, resource type, interaction), *outcomes* and *timings*.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Upstream:
    """One call the middleware made to a dependency (HAPI) while serving a request."""

    interaction: str  # read | search | create | update | delete | transaction | validate | metadata | other
    resource: str  # FHIR type, or "-" for system-level calls
    method: str
    status: int
    ms: float  # duration of the call
    start_ms: float  # offset from request start -> lets the UI draw a waterfall


@dataclass
class RequestLog:
    ts: str  # request start, ISO-8601 UTC
    request_id: str  # 16 hex chars; also sent to HAPI so it lands in hfj_res_ver.request_id
    method: str
    route: str  # template ("/validate"), never the raw path -> bounded cardinality
    status: int
    ms: float  # total server time
    category: str  # see categorize()
    bytes_out: int = 0
    actor: str = ""  # staff practitioner id (audit), set by annotate()
    role: str = ""
    op: str = ""  # business operation (e.g. "patient.admit"), set by annotate()
    outcome: str = ""  # free label from the handler: "submitted", "blocked", "payload_changed" ...
    error: str = ""  # exception class name if the handler blew up
    upstream: list[Upstream] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> RequestLog:
        d = dict(d)
        d["upstream"] = [Upstream(**u) for u in d.get("upstream", [])]
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})


CATEGORIES = ["ok", "validation", "conflict", "auth", "client_error", "upstream_error", "server_error"]


def categorize(status: int, error: str = "") -> str:
    """Bucket a response into a *cause*, which is what an on-call engineer actually triages by.

    HTTP status alone is too coarse: a 409 (someone else edited first) and a 422 (the form is wrong)
    are both "4xx" but need opposite reactions.
    """
    if error or status == 500:
        return "server_error"
    if status in (502, 503, 504):
        return "upstream_error"  # the dependency (HAPI) failed, not us
    if status in (401, 403):
        return "auth"
    if status in (409, 412):
        return "conflict"
    if status == 422:
        return "validation"
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "client_error"
    return "ok"


def classify_upstream(method: str, url: str, base: str) -> tuple[str, str]:
    """(interaction, resource) from a FHIR URL, with ids and query values dropped on purpose."""
    path = url.removeprefix(base.rstrip("/")).split("?", 1)[0].strip("/")
    has_query = "?" in url.removeprefix(base.rstrip("/"))
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ("transaction" if method == "POST" else "other"), "-"
    if parts[0] == "metadata":
        return "metadata", "-"
    rtype = parts[0].lstrip("$")
    if any(p.startswith("$") for p in parts[1:]):
        return "validate" if "$validate" in parts else "operation", rtype
    if method == "GET":
        if len(parts) == 1 or has_query:
            return "search", rtype
        return ("history" if "_history" in parts else "read"), rtype
    return {"POST": "create", "PUT": "update", "DELETE": "delete"}.get(method, "other"), rtype
