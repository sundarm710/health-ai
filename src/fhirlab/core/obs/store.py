"""LogStore port + a JSON-lines adapter.

JSONL is the lowest-friction production-shaped choice: append-only, greppable (`jq`), and every log
shipper (Vector, Promtail, Fluent Bit) can tail it. To move to Loki/Elastic/SQLite, implement `LogStore`
and `Sink` once; analytics and every UI keep working.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from pathlib import Path
from typing import Protocol

from fhirlab.core.obs.record import RequestLog

DEFAULT_PATH = Path(os.environ.get("FHIRLAB_LOG_PATH", "data/logs/middleware.jsonl"))


class LogStore(Protocol):
    def read(self, limit: int = 5000) -> list[RequestLog]:
        """Newest `limit` records, oldest first."""


class JsonlSink:
    """Write side. Thread-safe append of one JSON object per line."""

    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, rec: RequestLog) -> None:
        line = json.dumps(rec.to_dict(), separators=(",", ":"))
        with self._lock, self.path.open("a") as f:
            f.write(line + "\n")


class JsonlLogStore:
    """Read side."""

    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)

    def read(self, limit: int = 5000) -> list[RequestLog]:
        if not self.path.exists():
            return []
        with self.path.open() as f:
            tail = deque(f, maxlen=limit)
        out = []
        for line in tail:
            try:
                out.append(RequestLog.from_dict(json.loads(line)))
            except (ValueError, TypeError):
                continue  # a torn/garbled line must not hide the rest
        return out
