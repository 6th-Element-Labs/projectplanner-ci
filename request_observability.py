"""Process-local HTTP request latency instrumentation (PERF-7).

Complements HARDEN-49's MCP observability with web-path and webhook-ingest
latency histograms so SLO gates can defend webhook p99 < 50 ms and web p99
< 300 ms without relying on external APM.
"""
from __future__ import annotations

import math
import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _percentile(values, percentile: int) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return round(ordered[rank - 1], 3)


def classify_request_path(path: str, status_code: int = 200) -> str:
    """Bucket a request for SLO accounting."""
    if path == "/api/github/webhook":
        return "webhook_ingest"
    if path.startswith(("/api/", "/ixp/", "/txp/", "/tally/")):
        return "web"
    if path.startswith("/health"):
        return "health"
    return "other"


class RequestObservability:
    """Thread-safe, bounded collector for HTTP request latency."""

    def __init__(self, sample_limit: Optional[int] = None):
        self.sample_limit = sample_limit or _positive_int_env("PM_REQUEST_METRIC_SAMPLES", 2048)
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._latencies: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.sample_limit))
        self._counts: Dict[str, int] = defaultdict(int)
        self._failures: Dict[str, int] = defaultdict(int)
        self._dropped_webhooks = 0

    def record(self, route_class: str, elapsed_ms: float,
               status_code: int = 200, *, dropped_webhook: bool = False) -> None:
        elapsed_ms = round(max(0.0, elapsed_ms), 3)
        with self._lock:
            self._counts[route_class] += 1
            self._latencies[route_class].append(elapsed_ms)
            if status_code >= 500:
                self._failures[route_class] += 1
            if dropped_webhook:
                self._dropped_webhooks += 1

    def record_path(self, path: str, elapsed_ms: float, status_code: int = 200,
                    *, dropped_webhook: bool = False) -> None:
        self.record(classify_request_path(path, status_code), elapsed_ms,
                    status_code, dropped_webhook=dropped_webhook)

    def snapshot(self) -> dict:
        with self._lock:
            routes = {}
            for name in sorted(self._counts):
                samples = list(self._latencies[name])
                routes[name] = {
                    "calls": self._counts[name],
                    "failures": self._failures[name],
                    "retained_samples": len(samples),
                    "p50_ms": _percentile(samples, 50),
                    "p99_ms": _percentile(samples, 99),
                    "max_ms": round(max(samples), 3) if samples else None,
                }
            return {
                "schema": "switchboard.request_observability.v1",
                "process_started_at": round(self.started_at, 3),
                "uptime_s": round(max(0.0, time.time() - self.started_at), 3),
                "sample_limit_per_route": self.sample_limit,
                "dropped_webhook_deliveries": self._dropped_webhooks,
                "routes": routes,
            }
