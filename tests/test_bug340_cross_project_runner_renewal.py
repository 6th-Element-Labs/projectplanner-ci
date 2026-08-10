#!/usr/bin/env python3
"""BUG-340: renew live runners in the project that owns their lease."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


PRIMARY = agent_host.PROJECT
TARGET = "switchboard" if PRIMARY != "switchboard" else "maxwell"
RUNNER_ID = "run-bug340-target"

inventory = {
    "host_id": "host/bug340",
    "repo_root": str(ROOT),
    "placement": {"projects": [PRIMARY, TARGET]},
}
target_runner = {
    "runner_session_id": RUNNER_ID,
    "host_id": "host/bug340",
    "agent_id": "agent/codex/bug-340",
    "runtime": "codex",
    "task_id": "BUG-340",
    "pid": 340,
    "alive": True,
    "status": "running",
    "cwd": str(ROOT),
    "metadata": {
        "connect_assignment": True,
        "native_host_execution": True,
        "wake_id": "wake-bug340",
        "execution_id": "execlease-bug340",
        "execution_generation": 1,
        "execution_role": "implementation",
        "lease_epoch": 1,
    },
}

drained_projects = []
heartbeats = []
saved_drain = agent_host._drain_runners
saved_work_sessions = agent_host._drain_work_sessions
saved_try = agent_host._try
try:
    def fake_drain(_host_id, *args, **kwargs):
        project = kwargs.get("project", PRIMARY)
        drained_projects.append(project)
        return [target_runner] if project == TARGET else []

    agent_host._drain_runners = fake_drain
    agent_host._drain_work_sessions = lambda **_kwargs: []

    def fake_try(method, path, body=None, timeout=None):
        del timeout
        if method == "POST" and path == agent_host.P_HEARTBEAT_RUNNER:
            heartbeats.append(dict(body or {}))
            return {"runner_session_id": RUNNER_ID}
        return {}

    agent_host._try = fake_try
    renewed = agent_host.renew_live_direct_runners(inventory)
finally:
    agent_host._drain_runners = saved_drain
    agent_host._drain_work_sessions = saved_work_sessions
    agent_host._try = saved_try

assert drained_projects == [PRIMARY, TARGET], drained_projects
assert len(heartbeats) == 1, heartbeats
assert heartbeats[0]["project"] == TARGET, heartbeats[0]
assert renewed == [{
    "runner_session_id": RUNNER_ID,
    "task_id": "BUG-340",
    "renewed": True,
    "error": None,
    "renew_deferred": False,
    "relay_url_minted": False,
    "server_relay_error": None,
    "server_relay_missing": [],
}], renewed

print("BUG-340 cross-project runner renewal: PASS")
