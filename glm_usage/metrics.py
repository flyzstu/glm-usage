"""Cheap in-process counters exposed through ``/api/v1/metrics``.

Counters are per worker process: with ``GLM_USAGE_WORKERS > 1`` each worker
reports its own slice, which is enough for eyeballing cache efficiency.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any


class Metrics:
    __slots__ = ("started_at", "_requests", "_upstream_calls", "_upstream_errors", "_upstream_ms")

    def __init__(self) -> None:
        self.started_at = time.time()
        self._requests: Counter[str] = Counter()
        self._upstream_calls = 0
        self._upstream_errors = 0
        self._upstream_ms = 0.0

    def record_request(self, route: str, status: int) -> None:
        self._requests[f"{route}|{status}"] += 1

    def record_cache(self, state: str) -> None:
        self._requests[f"cache:{state}"] += 1

    def record_upstream(self, elapsed_ms: float, *, error: bool = False) -> None:
        self._upstream_calls += 1
        self._upstream_ms += elapsed_ms
        if error:
            self._upstream_errors += 1

    def snapshot(self) -> dict[str, Any]:
        calls = self._upstream_calls
        return {
            "uptimeSeconds": round(time.time() - self.started_at, 3),
            "requests": {key: value for key, value in self._requests.items() if "|" in key},
            "cache": {
                key.split(":", 1)[1]: value
                for key, value in self._requests.items()
                if key.startswith("cache:")
            },
            "upstream": {
                "calls": calls,
                "errors": self._upstream_errors,
                "avgLatencyMs": round(self._upstream_ms / calls, 2) if calls else 0.0,
            },
        }
