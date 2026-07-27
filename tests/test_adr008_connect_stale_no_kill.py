#!/usr/bin/env python3
"""ADR-0008 C2: stale alone must not kill a still-alive Connect/native runner."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


calls = []
old_drain = agent_host._drain_runners
old_action = agent_host.supervisor_action
old_try = agent_host._try
old_drop = agent_host._drop_host_bridge
try:
    agent_host._drain_runners = lambda _host_id: [{
        "runner_session_id": "run-connect-stale",
        "host_id": "host/test",
        "task_id": "ADAPTER-36",
        "agent_id": "codex/ADAPTER-36",
        "wake_mode": "connect",
        "alive": True,
        "stale": True,
        "status": "running",
        "metadata": {
            "native_host_execution": True,
            "connect_assignment": True,
            "wake_id": "wake-connect-stale",
        },
    }]
    agent_host.supervisor_action = lambda action, runner_id, options=None: (
        calls.append((action, runner_id, options)) or
        {"alive": False, "status": "killed"})
    agent_host._drop_host_bridge = lambda runner_id: calls.append(
        ("drop", runner_id))
    agent_host._try = lambda method, path, body=None: (
        calls.append((method, path, body)) or {"ok": True})
    result = agent_host.expire_runner_leases(
        {"host_id": "host/test"}, now=10_000)
finally:
    agent_host._drain_runners = old_drain
    agent_host.supervisor_action = old_action
    agent_host._try = old_try
    agent_host._drop_host_bridge = old_drop

assert result == [], "alive Connect+stale without surrender is spared"
assert not any(c[:1] == ("kill",) for c in calls), (
    "Capacity must not SIGTERM a live Connect PTY on stale alone")

# Surrender still kills immediately (BUG-175 / complete_claim fence).
calls.clear()
old_drain = agent_host._drain_runners
old_action = agent_host.supervisor_action
old_try = agent_host._try
old_drop = agent_host._drop_host_bridge
try:
    agent_host._drain_runners = lambda _host_id: [{
        "runner_session_id": "run-connect-surrender",
        "host_id": "host/test",
        "task_id": "ADAPTER-36",
        "agent_id": "codex/ADAPTER-36",
        "wake_mode": "connect",
        "alive": True,
        "stale": False,
        "status": "running",
        "metadata": {
            "native_host_execution": True,
            "connect_assignment": True,
            "lease_surrender": {"authority": "complete_claim"},
        },
    }]
    agent_host.supervisor_action = lambda action, runner_id, options=None: (
        calls.append((action, runner_id, options)) or
        {"alive": False, "status": "killed"})
    agent_host._drop_host_bridge = lambda runner_id: calls.append(
        ("drop", runner_id))
    agent_host._try = lambda method, path, body=None: (
        calls.append((method, path, body)) or {"ok": True})
    surrendered = agent_host.expire_runner_leases(
        {"host_id": "host/test"}, now=10_000)
finally:
    agent_host._drain_runners = old_drain
    agent_host.supervisor_action = old_action
    agent_host._try = old_try
    agent_host._drop_host_bridge = old_drop

assert surrendered and surrendered[0].get("reason") == "runner_lease_surrendered"
assert calls[0][:2] == ("kill", "run-connect-surrender")

print("ADR-0008 Connect stale-no-kill: PASS")
