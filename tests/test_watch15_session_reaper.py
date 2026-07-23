#!/usr/bin/env python3
"""The renewable execution lease is the only automatic runner stop clock."""
from pathlib import Path

from path_setup import ROOT  # noqa: F401,E402
from adapters import agent_host  # noqa: E402

assert not hasattr(agent_host, "reap_finished_or_idle_runners")
assert not hasattr(agent_host, "runner_lease_enforcement_enabled")
assert {"execution_lease_v2", "runner_lease_enforcement"}.issubset(
    set(agent_host.default_inventory()["runtimes"][0]["capabilities"]))

old_drain = agent_host._drain_runners
old_action = agent_host.supervisor_action
old_try = agent_host._try
old_drop = agent_host._drop_host_bridge
calls = []
try:
    agent_host._drain_runners = lambda _host_id: [{
        "runner_session_id": "lease-expired",
        "host_id": "host/test",
        "task_id": "WATCH-15",
        "agent_id": "codex/WATCH-15",
        "alive": True,
        "stale": True,
        "status": "running",
        "metadata": {"work_session_id": "worksession-expired"},
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

assert result == [{
    "runner_session_id": "lease-expired",
    "task_id": "WATCH-15",
    "reason": "runner_lease_expired",
    "expired": True,
}]
assert calls[0][:2] == ("kill", "lease-expired")
terminal = next(call[2] for call in calls
                if call[:2] == ("POST", agent_host.P_HEARTBEAT_RUNNER))
assert terminal["status"] == "expired"
assert terminal["metadata"]["terminalized_by"] == "runner_lease_expiry"

print("WATCH-15 lease-only reaper: PASS")
