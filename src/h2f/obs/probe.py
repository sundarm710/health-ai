"""Active latency probes: what an outside monitor measures, independent of real traffic.

Passive logs tell you how real requests behaved; probes tell you whether the path is healthy *right now*
(and the only signal you have when there is no traffic). We measure connection setup and a warm request
separately, because network cost (DNS/TCP) and server cost (handler time) fail differently.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

import httpx


@dataclass
class ProbeResult:
    target: str
    url: str
    ok: bool
    cold_ms: float  # new connection + request (connect cost included)
    warm_ms: list[float]  # reused connection: pure request/response
    status: int = 0
    error: str = ""

    @property
    def warm_p50(self) -> float:
        return round(statistics.median(self.warm_ms), 1) if self.warm_ms else 0.0

    @property
    def network_ms(self) -> float:
        """Rough connect overhead: cold minus warm median."""
        return round(max(self.cold_ms - self.warm_p50, 0), 1)


def probe(target: str, url: str, warm: int = 5, timeout: float = 3.0) -> ProbeResult:
    try:
        t0 = time.perf_counter()
        with httpx.Client(timeout=timeout) as c:
            r = c.get(url)
            cold = (time.perf_counter() - t0) * 1000
            samples = []
            for _ in range(warm):
                t = time.perf_counter()
                c.get(url)
                samples.append((time.perf_counter() - t) * 1000)
        return ProbeResult(target, url, r.status_code < 500, round(cold, 1), [round(s, 1) for s in samples], r.status_code)
    except httpx.HTTPError as e:
        return ProbeResult(target, url, False, 0.0, [], error=type(e).__name__)
