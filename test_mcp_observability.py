#!/usr/bin/env python3
"""HARDEN-49 — hermetic MCP latency/lock-wait instrumentation tests."""
import asyncio
import inspect
import sqlite3

from mcp_observability import MCPObservability, _percentile


def check(condition, message):
    if not condition:
        raise AssertionError(message)


check(_percentile([1, 2, 3, 4, 5], 50) == 3, "p50 uses nearest rank")
check(_percentile([1, 2, 3, 4, 5], 99) == 5, "p99 uses nearest rank")

obs = MCPObservability(sample_limit=3, slow_log_limit=2, slow_call_ms=10)
obs.record("read", 1)
obs.record("read", 2)
obs.record("read", 3)
obs.record("read", 100)
snap = obs.snapshot()
check(snap["tools"]["read"]["calls"] == 4, "total calls outlive bounded samples")
check(snap["tools"]["read"]["retained_samples"] == 3, "latency samples are bounded")
check(snap["tools"]["read"]["p50_ms"] == 3, "p50 reflects retained window")
check(snap["tools"]["read"]["p99_ms"] == 100, "p99 reflects retained window")

obs.record("locked", 25, sqlite3.OperationalError("database is locked"))
snap = obs.snapshot()
check(snap["sqlite_lock_waits"] == 1, "SQLite busy/locked failures increment counter")
check(snap["tools"]["locked"]["failures"] == 1, "failed tool is counted")
check(len(snap["slow_calls"]) == 2, "slow-call log is bounded")
check(set(snap["slow_calls"][1]) == {
    "at", "tool", "elapsed_ms", "ok", "error_type", "sqlite_lock_wait"
}, "slow log contains metadata only, never arguments/results")


def sync_tool(secret: str = "not-recorded"):
    """schema doc"""
    return secret


wrapped = obs.wrap(sync_tool)
check(wrapped("sensitive") == "sensitive", "sync wrapper preserves return value")
check(inspect.signature(wrapped) == inspect.signature(sync_tool), "sync signature is preserved")


async def async_tool(value: int):
    return value + 1


async_wrapped = obs.wrap(async_tool)
check(asyncio.run(async_wrapped(2)) == 3, "async wrapper preserves return value")
check(inspect.signature(async_wrapped) == inspect.signature(async_tool), "async signature is preserved")


@obs.wrap
def failing_tool():
    raise ValueError("original")


try:
    failing_tool()
except ValueError as exc:
    check(str(exc) == "original", "wrapper re-raises original exception")
else:
    raise AssertionError("wrapper swallowed original exception")

filtered = obs.snapshot(tool="read", slow_limit=0)
check(set(filtered["tools"]) == {"read"}, "exact tool filter is honored")
check(filtered["slow_calls"] == [], "slow_limit=0 suppresses log entries")

print("MCP observability tests passed")
