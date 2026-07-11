"""Process-local latency and SQLite contention instrumentation for MCP tools.

The collector deliberately stores no tool arguments or return values.  Metrics are
bounded in memory and disappear on process restart; they are an operational signal,
not a second audit log.

HARDEN-63 (Bar-2 observability) extends the collector so the agent-path SLO is
continuously observable rather than a one-off measurement:

  * per-tool SQLite lock-wait counts — attributes write contention to the exact
    tool that hit it, so a regression is legible before it hurts.  Fed both by the
    lock-wait errors that escape the store's retry loop AND by the store retry loop
    itself via ``note_sqlite_lock_wait`` (see db.core.register_lock_wait_observer),
    which is where most transient contention is actually observed and retried away.
  * write-path latency p50/p99 — a distinct histogram for calls that took the
    Switchboard write path (marked via ``mark_write`` from the shared _require_write
    gate), since writes are the contended, SLO-relevant operations.

This complements the HTTP server-timing headers in mcp_http_timing.
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
from typing import Callable, Deque, Dict, List, Optional


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
        self._write_latencies: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.sample_limit))
        self._counts: Dict[str, int] = defaultdict(int)
        self._write_counts: Dict[str, int] = defaultdict(int)
        self._failures: Dict[str, int] = defaultdict(int)
        self._lock_waits: Dict[str, int] = defaultdict(int)
        self._sqlite_lock_waits = 0
        self._slow_calls: Deque[dict] = deque(maxlen=self.slow_log_limit)
        # Per-thread call context. The dispatcher runs each sync tool on one worker
        # thread for the whole call, so a thread-local set by wrap() is visible to
        # deeper signals (write marking, store lock-wait retries) on the same stack.
        self._ctx = threading.local()

    def _current_tool(self) -> Optional[str]:
        return getattr(self._ctx, "tool", None)

    def mark_write(self) -> None:
        """Flag the in-flight tool call as taking the write path.

        Called from the shared _require_write gate so every write-authorized tool
        contributes to the write-latency histogram without touching ~150 handlers."""
        self._ctx.write = True

    def note_sqlite_lock_wait(self, tool: Optional[str] = None) -> None:
        """Record one SQLite lock-wait, attributed to ``tool`` (or the in-flight tool).

        This is the observer the store's retry loop calls (db.core), so contention
        that is transparently retried away is still counted and attributable."""
        name = tool or self._current_tool()
        with self._lock:
            self._sqlite_lock_waits += 1
            if name:
                self._lock_waits[name] += 1

    def record(self, tool: str, elapsed_ms: float,
               error: Optional[BaseException] = None, write: bool = False) -> None:
        elapsed_ms = round(max(0.0, elapsed_ms), 3)
        lock_wait = _sqlite_busy(error)
        with self._lock:
            self._counts[tool] += 1
            self._latencies[tool].append(elapsed_ms)
            if write:
                self._write_counts[tool] += 1
                self._write_latencies[tool].append(elapsed_ms)
            if error is not None:
                self._failures[tool] += 1
            if lock_wait:
                # A lock-wait that escaped the store's retry loop (or a write not
                # covered by it). Retried-away contention arrives via
                # note_sqlite_lock_wait.
                self._sqlite_lock_waits += 1
                self._lock_waits[tool] += 1
            if elapsed_ms >= self.slow_call_ms:
                self._slow_calls.append({
                    "at": round(time.time(), 3),
                    "tool": tool,
                    "elapsed_ms": elapsed_ms,
                    "ok": error is None,
                    "error_type": type(error).__name__ if error is not None else None,
                    "sqlite_lock_wait": lock_wait,
                    "write": bool(write),
                })

    def wrap(self, fn: Callable) -> Callable:
        """Wrap a sync or async FastMCP tool while preserving its public signature."""
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_observed(*args, **kwargs):
                started = time.perf_counter()
                error = None
                prev_tool = getattr(self._ctx, "tool", None)
                prev_write = getattr(self._ctx, "write", False)
                self._ctx.tool = fn.__name__
                self._ctx.write = False
                try:
                    return await fn(*args, **kwargs)
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    write = getattr(self._ctx, "write", False)
                    self._ctx.tool = prev_tool
                    self._ctx.write = prev_write
                    self.record(fn.__name__, (time.perf_counter() - started) * 1000.0,
                                error, write=write)
            return async_observed

        @functools.wraps(fn)
        def observed(*args, **kwargs):
            started = time.perf_counter()
            error = None
            prev_tool = getattr(self._ctx, "tool", None)
            prev_write = getattr(self._ctx, "write", False)
            self._ctx.tool = fn.__name__
            self._ctx.write = False
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:
                error = exc
                raise
            finally:
                write = getattr(self._ctx, "write", False)
                self._ctx.tool = prev_tool
                self._ctx.write = prev_write
                self.record(fn.__name__, (time.perf_counter() - started) * 1000.0,
                            error, write=write)
        return observed

    def snapshot(self, tool: str = "", slow_limit: int = 50) -> dict:
        slow_limit = max(0, min(int(slow_limit), self.slow_log_limit))
        with self._lock:
            if tool:
                names = [tool]
            else:
                names = sorted(set(self._counts) | set(self._lock_waits)
                               | set(self._write_counts))
            tools = {}
            write_samples: List[float] = []
            write_calls_total = 0
            for name in names:
                if not (name in self._counts or name in self._lock_waits
                        or name in self._write_counts):
                    continue
                samples = list(self._latencies[name])
                wsamples = list(self._write_latencies[name])
                write_samples.extend(wsamples)
                write_calls_total += self._write_counts.get(name, 0)
                tools[name] = {
                    "calls": self._counts.get(name, 0),
                    "failures": self._failures.get(name, 0),
                    "sqlite_lock_waits": self._lock_waits.get(name, 0),
                    "retained_samples": len(samples),
                    "p50_ms": _percentile(samples, 50),
                    "p99_ms": _percentile(samples, 99),
                    "max_ms": round(max(samples), 3) if samples else None,
                    "write_calls": self._write_counts.get(name, 0),
                    "write_retained_samples": len(wsamples),
                    "write_p50_ms": _percentile(wsamples, 50),
                    "write_p99_ms": _percentile(wsamples, 99),
                }
            slow = list(self._slow_calls)
            if tool:
                slow = [item for item in slow if item["tool"] == tool]
            if slow_limit == 0:
                slow = []
            else:
                slow = slow[-slow_limit:]
            return {
                "schema": "switchboard.mcp_observability.v2",
                "process_started_at": round(self.started_at, 3),
                "uptime_s": round(max(0.0, time.time() - self.started_at), 3),
                "slow_call_threshold_ms": self.slow_call_ms,
                "sample_limit_per_tool": self.sample_limit,
                "sqlite_lock_waits": self._sqlite_lock_waits,
                "writes": {
                    "calls": write_calls_total,
                    "retained_samples": len(write_samples),
                    "p50_ms": _percentile(write_samples, 50),
                    "p99_ms": _percentile(write_samples, 99),
                },
                "tools": tools,
                "slow_calls": slow,
            }
