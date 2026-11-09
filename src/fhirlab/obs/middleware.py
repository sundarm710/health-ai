"""ASGI middleware: one structured RequestLog per request.

Why pure ASGI (not Starlette's BaseHTTPMiddleware): no body buffering, no extra task, and the
contextvar set here is visible to the endpoint and to calls it makes in worker threads.

Handlers add business meaning with `annotate(op=..., outcome=...)` and report dependency calls with
`record_upstream(...)`; the middleware never reads bodies.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fhirlab.obs.record import RequestLog, Upstream, categorize, classify_upstream

Sink = Callable[[RequestLog], None]
REQUEST_ID_HEADER = "x-request-id"


@dataclass
class _Ctx:
    request_id: str
    t0: float
    attrs: dict = field(default_factory=dict)
    upstream: list[Upstream] = field(default_factory=list)


_ctx: ContextVar[_Ctx | None] = ContextVar("fhirlab_request_ctx", default=None)


def current_request_id() -> str | None:
    c = _ctx.get()
    return c.request_id if c else None


def annotate(**attrs) -> None:
    """Attach business fields (op, outcome, actor, role) to the request being served. No-op outside a request."""
    if c := _ctx.get():
        c.attrs.update({k: v for k, v in attrs.items() if v is not None})


def record_upstream(call: dict, base: str) -> None:
    """Feed from FhirClient(on_call=...): {method, url, status, ms}."""
    c = _ctx.get()
    if not c:
        return
    interaction, resource = classify_upstream(call["method"], call["url"], base)
    ms = float(call.get("ms", 0))
    end = (time.perf_counter() - c.t0) * 1000
    c.upstream.append(Upstream(interaction, resource, call["method"], int(call["status"]), ms, round(max(end - ms, 0), 1)))


class RequestLogMiddleware:
    def __init__(self, app, sink: Sink, *, skip_prefixes: tuple[str, ...] = ("/health", "/ops", "/docs", "/openapi.json")):
        self.app, self.sink, self.skip = app, sink, skip_prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(self.skip):
            return await self.app(scope, receive, send)

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.encode(), b"").decode()
        rid = incoming if incoming.isalnum() and len(incoming) <= 16 else uuid.uuid4().hex[:16]
        ctx = _Ctx(rid, time.perf_counter())
        token = _ctx.set(ctx)
        started = datetime.now(UTC)
        status, size, error = 500, 0, ""

        async def send_wrapper(msg):
            nonlocal status, size
            if msg["type"] == "http.response.start":
                status = msg["status"]
                msg["headers"] = [*msg.get("headers", []), (REQUEST_ID_HEADER.encode(), rid.encode())]
            elif msg["type"] == "http.response.body":
                size += len(msg.get("body", b""))
            await send(msg)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            error = type(e).__name__
            raise
        finally:
            ms = round((time.perf_counter() - ctx.t0) * 1000, 1)
            route = getattr(scope.get("route"), "path", None) or "(unmatched)"
            a = ctx.attrs
            try:
                self.sink(RequestLog(
                    ts=started.isoformat(timespec="milliseconds"), request_id=rid, method=scope["method"], route=route,
                    status=status, ms=ms, category=categorize(status, error), bytes_out=size,
                    actor=a.get("actor", ""), role=a.get("role", ""), op=a.get("op", ""), outcome=a.get("outcome", ""),
                    error=error, upstream=ctx.upstream,
                ))
            except Exception:  # logging must never take the request down
                pass
            _ctx.reset(token)
