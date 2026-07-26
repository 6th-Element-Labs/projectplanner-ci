#!/usr/bin/env python3
"""ADAPTER-35: Connect runners get fair leases and visible renewal failures."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


inventory = {"host_id": "host/adapter-35", "repo_root": str(ROOT)}
wake = {
    "wake_id": "wake-adapter-35",
    "task_id": "ADAPTER-35",
    "selector": {"agent_id": "agent/codex/adapter-35", "runtime": "codex"},
    "policy": {
        "mode": "connect",
        "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            "assignment_id": "assignment-adapter-35",
        },
        "lifecycle": {"execution_id": "execution-adapter-35", "generation": 1},
    },
}
record = {
    "runner_session_id": "run-adapter-35",
    "agent_id": "agent/codex/adapter-35",
    "runtime": "codex",
    "task_id": "ADAPTER-35",
    "status": "running",
    "wake_mode": "connect",
    "metadata": {"native_host_execution": True, "wake_id": "wake-adapter-35"},
}

registered = []
saved_require = agent_host._require
try:
    agent_host._require = lambda method, path, body=None: (
        registered.append((method, path, dict(body or {})))
        or {"runner_session_id": "run-adapter-35"}
    )
    agent_host.register_runner_session(record, wake, inventory)
finally:
    agent_host._require = saved_require

assert registered[0][2]["heartbeat_ttl_s"] == 180

attempts = []
saved_try = agent_host._try
saved_drain = agent_host._drain_runners
saved_work_sessions = agent_host._drain_work_sessions
try:
    agent_host._drain_runners = lambda _host_id: [{
        **record,
        "host_id": "host/adapter-35",
        "pid": 35,
        "alive": True,
        "cwd": str(ROOT),
    }]
    agent_host._drain_work_sessions = lambda **_kwargs: []

    def fail_heartbeat(method, path, body=None):
        attempts.append((method, path, dict(body or {})))
        return {"error": "temporary transport failure"}

    agent_host._try = fail_heartbeat
    renewed = agent_host.renew_live_direct_runners(inventory)
finally:
    agent_host._try = saved_try
    agent_host._drain_runners = saved_drain
    agent_host._drain_work_sessions = saved_work_sessions

heartbeat_attempts = [
    call for call in attempts
    if call[0:2] == ("POST", agent_host.P_HEARTBEAT_RUNNER)
]
assert len(heartbeat_attempts) == 2
assert renewed == [{
    "runner_session_id": "run-adapter-35",
    "task_id": "ADAPTER-35",
    "renewed": False,
    "error": "temporary transport failure",
    "renew_deferred": True,
    "relay_url_minted": False,
    "server_relay_error": None,
    "server_relay_missing": [],
}]

print("ADAPTER-35 runner lease retry: 3 passed, 0 failed")
