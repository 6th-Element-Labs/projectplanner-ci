#!/usr/bin/env python3
"""HARDEN-49 / HARDEN-63 — hermetic MCP latency/lock-wait instrumentation tests."""
import asyncio
import inspect
import json
import sqlite3

from mcp_observability import MCPObservability, _percentile
from mcp_observability_http import MCPObservabilityEndpoint


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
check(snap["tools"]["locked"]["sqlite_lock_waits"] == 1,
      "HARDEN-63: lock-waits are attributed per tool")
check(snap["tools"]["read"]["sqlite_lock_waits"] == 0,
      "HARDEN-63: an unrelated tool shows no lock-waits")
check(len(snap["slow_calls"]) == 2, "slow-call log is bounded")
check(set(snap["slow_calls"][1]) == {
    "at", "tool", "elapsed_ms", "ok", "error_type", "sqlite_lock_wait", "write"
}, "slow log contains metadata only, never arguments/results")

# HARDEN-63: write-path latency histogram (per-tool + aggregate).
wobs = MCPObservability(sample_limit=10, slow_log_limit=2, slow_call_ms=10_000)
wobs.record("update_task", 5, write=True)
wobs.record("update_task", 7, write=True)
wobs.record("update_task", 9, write=True)
wobs.record("get_task", 3)  # read — must not enter the write histogram
wsnap = wobs.snapshot()
check(wsnap["tools"]["update_task"]["write_calls"] == 3, "per-tool write calls counted")
check(wsnap["tools"]["update_task"]["write_p50_ms"] == 7, "per-tool write p50")
check(wsnap["tools"]["update_task"]["write_p99_ms"] == 9, "per-tool write p99")
check(wsnap["tools"]["get_task"]["write_calls"] == 0, "reads never count as writes")
check(wsnap["tools"]["get_task"]["write_p50_ms"] is None, "reads have no write latency")
check(wsnap["writes"]["calls"] == 3, "aggregate write calls")
check(wsnap["writes"]["p99_ms"] == 9, "aggregate write p99")

# HARDEN-63: mark_write() from inside a wrapped call classifies it as a write.
def writer_tool():
    wobs.mark_write()
    return "ok"

check(wobs.wrap(writer_tool)() == "ok", "wrapped writer preserves return value")
check(wobs.snapshot()["tools"]["writer_tool"]["write_calls"] == 1,
      "mark_write() inside wrap classifies the call as a write")

# HARDEN-63: note_sqlite_lock_wait() attributes contention to the in-flight tool.
lobs = MCPObservability(sample_limit=10, slow_log_limit=2, slow_call_ms=10_000)
lobs.note_sqlite_lock_wait("mark_task_merged")
check(lobs.snapshot()["sqlite_lock_waits"] == 1, "explicit lock-wait bumps the total")
check(lobs.snapshot()["tools"]["mark_task_merged"]["sqlite_lock_waits"] == 1,
      "explicit lock-wait attributes to the named tool")

seen = {}
def contended_tool():
    # Simulates the store retry loop firing while this tool is in flight.
    lobs.note_sqlite_lock_wait()
    return "done"

lobs.wrap(contended_tool)()
check(lobs.snapshot()["tools"]["contended_tool"]["sqlite_lock_waits"] == 1,
      "in-flight lock-wait attributes to the running tool via thread-local context")


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


# HARDEN-63: plain-HTTP operator endpoint (GET /observability) — hermetic ASGI drive.
def _drive(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


passthrough_calls = []

async def _passthrough(scope, receive, send):
    passthrough_calls.append(scope.get("path"))
    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b""})

endpoint = MCPObservabilityEndpoint(_passthrough, obs.snapshot)

resp = _drive(endpoint, {
    "type": "http", "method": "GET", "path": "/observability",
    "query_string": b"tool=read&slow_limit=0",
})
check(resp[0]["status"] == 200, "GET /observability returns 200")
check(dict(resp[0]["headers"]).get(b"content-type") == b"application/json",
      "observability endpoint is JSON")
body = json.loads(resp[1]["body"])
check(body["schema"] == "switchboard.mcp_observability.v2", "endpoint serves the snapshot")
check(set(body["tools"]) == {"read"}, "endpoint forwards the tool query filter")
check(passthrough_calls == [], "GET /observability is handled, not passed through")

# Non-matching requests fall through to the wrapped app untouched (e.g. /mcp).
_drive(endpoint, {"type": "http", "method": "POST", "path": "/mcp", "query_string": b""})
_drive(endpoint, {"type": "http", "method": "GET", "path": "/health", "query_string": b""})
check(passthrough_calls == ["/mcp", "/health"],
      "only GET /observability is intercepted; everything else passes through")

print("MCP observability tests passed")
