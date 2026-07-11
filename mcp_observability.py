"""Process-local latency and SQLite contention instrumentation for MCP tools.

The collector deliberately stores no tool arguments or return values.  Metrics are
bounded in memory and disappear on process restart; they are an operational signal,
not a second audit log.
"""
from __future__ import annotations

import functools
import inspect
import math
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Optional


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _positive_float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _percentile(values, percentile: int) -> Optional[float]:
    """Nearest-rank percentile, appropriate for an operational latency histogram."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return round(ordered[rank - 1], 3)


def _sqlite_busy(exc: Optional[BaseException]) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text or "locked" in text


class MCPObservability:
    """Thread-safe, bounded collector for MCP tool-call instrumentation."""

    def __init__(self, sample_limit: Optional[int] = None,
                 slow_log_limit: Optional[int] = None,
                 slow_call_ms: Optional[float] = None):
        self.sample_limit = sample_limit or _positive_int_env("PM_MCP_METRIC_SAMPLES", 2048)
        self.slow_log_limit = slow_log_limit or _positive_int_env("PM_MCP_SLOW_LOG_LIMIT", 200)
        self.slow_call_ms = (slow_call_ms if slow_call_ms is not None else
                             _positive_float_env("PM_MCP_SLOW_CALL_MS", 1000.0))
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._latencies: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.sample_limit))
        self._counts: Dict[str, int] = defaultdict(int)
        self._failures: Dict[str, int] = defaultdict(int)
        self._sqlite_lock_waits = 0
        self._slow_calls: Deque[dict] = deque(maxlen=self.slow_log_limit)

    def record(self, tool: str, elapsed_ms: float,
               error: Optional[BaseException] = None) -> None:
        elapsed_ms = round(max(0.0, elapsed_ms), 3)
        lock_wait = _sqlite_busy(error)
        with self._lock:
            self._counts[tool] += 1
            self._latencies[tool].append(elapsed_ms)
            if error is not None:
                self._failures[tool] += 1
            if lock_wait:
                self._sqlite_lock_waits += 1
            if elapsed_ms >= self.slow_call_ms:
                self._slow_calls.append({
                    "at": round(time.time(), 3),
                    "tool": tool,
                    "elapsed_ms": elapsed_ms,
                    "ok": error is None,
                    "error_type": type(error).__name__ if error is not None else None,
                    "sqlite_lock_wait": lock_wait,
                })

    def wrap(self, fn: Callable) -> Callable:
        """Wrap a sync or async FastMCP tool while preserving its public signature."""
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_observed(*args, **kwargs):
                started = time.perf_counter()
                error = None
                try:
                    return await fn(*args, **kwargs)
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    self.record(fn.__name__, (time.perf_counter() - started) * 1000.0, error)
            return async_observed

        @functools.wraps(fn)
        def observed(*args, **kwargs):
            started = time.perf_counter()
            error = None
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:
                error = exc
                raise
            finally:
                self.record(fn.__name__, (time.perf_counter() - started) * 1000.0, error)
        return observed

    def snapshot(self, tool: str = "", slow_limit: int = 50) -> dict:
        slow_limit = max(0, min(int(slow_limit), self.slow_log_limit))
        with self._lock:
            names = [tool] if tool else sorted(self._counts)
            tools = {}
            for name in names:
                if name not in self._counts:
                    continue
                samples = list(self._latencies[name])
                tools[name] = {
                    "calls": self._counts[name],
                    "failures": self._failures[name],
                    "retained_samples": len(samples),
                    "p50_ms": _percentile(samples, 50),
                    "p99_ms": _percentile(samples, 99),
                    "max_ms": round(max(samples), 3) if samples else None,
                }
            slow = list(self._slow_calls)
            if tool:
                slow = [item for item in slow if item["tool"] == tool]
            if slow_limit == 0:
                slow = []
            else:
                slow = slow[-slow_limit:]
            return {
                "schema": "switchboard.mcp_observability.v1",
                "process_started_at": round(self.started_at, 3),
                "uptime_s": round(max(0.0, time.time() - self.started_at), 3),
                "slow_call_threshold_ms": self.slow_call_ms,
                "sample_limit_per_tool": self.sample_limit,
                "sqlite_lock_waits": self._sqlite_lock_waits,
                "tools": tools,
                "slow_calls": slow,
            }
