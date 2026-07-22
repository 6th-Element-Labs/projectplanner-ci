#!/usr/bin/env python3
"""UI-37: claim-bound launches finalize independently of host polling."""
from __future__ import annotations

import os
import threading
import time

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


host_id = "host/ui37"
wakes = [{
    "wake_id": f"wake-ui37-{index}",
    "task_id": f"UI-37-{index}",
    "selector": {
        "runtime": "codex",
        "agent_id": f"codex/UI-37-{index}",
        "lane": "UI",
    },
    "policy": {"mode": "claim_next", "require_runner_bind": True},
} for index in range(3)]
inventory = {
    "host_id": host_id,
    "repo_root": "/tmp/ui37-source",
    "limits": {"max_sessions": 8},
    "policy": {"allow_work": True, "allow_global_claim": False},
    "runtimes": [{"runtime": "codex", "lanes": ["UI"]}],
}
release = {wake["wake_id"]: threading.Event() for wake in wakes}
launches = []
completions = []
heartbeats = []
control_polls = []
list_calls = [0]
kills = []

originals = {
    name: getattr(agent_host, name)
    for name in (
        "_try", "heartbeat_capacity", "apply_authoritative_execution_policy",
        "handle_runner_controls", "active_session_count", "launch",
        "confirm_started", "_register_preclaim_runner",
        "register_runner_session", "wait_for_runner_binding",
        "supervisor_action",
    )
}


def fake_try(method, path, body=None, timeout=None):
    del timeout
    if path == agent_host.P_HEARTBEAT_HOST:
        heartbeats.append(dict(body or {}))
        return {"ok": True}
    if path.startswith(agent_host.P_LIST_WAKES):
        list_calls[0] += 1
        return {"wake_intents": wakes if list_calls[0] == 1 else []}
    if path == agent_host.P_CLAIM_WAKE:
        wake = next(row for row in wakes if row["wake_id"] == body["wake_id"])
        return {"claimed": True, "wake": wake}
    if path == agent_host.P_COMPLETE_WAKE:
        completions.append(dict(body or {}))
        return {"status": "completed"}
    return {"ok": True}


def fake_launch(wake, _inventory, runner_session_id=None, extra_env=None):
    del extra_env
    launches.append(wake["wake_id"])
    return {
        "started": True,
        "runner_session_id": runner_session_id,
        "pid": 37000 + len(launches),
        "cwd": f"/tmp/{wake['task_id']}",
        "wake_mode": "claim_next",
        "pty": True,
        "control": {"runner_open": True, "runner_inject": True},
        "metadata": {"credential_admission_phase": "preclaim"},
    }


def fake_wait(wake, _inventory, runner_session_id, **_kwargs):
    release[wake["wake_id"]].wait(timeout=5)
    if wake["wake_id"].endswith("-2"):
        return {"bound": False, "reason": "runner_bind_timeout"}
    return {
        "bound": True,
        "session": {
            "runner_session_id": runner_session_id,
            "host_id": host_id,
            "agent_id": wake["selector"]["agent_id"],
            "runtime": "codex",
            "task_id": wake["task_id"],
            "claim_id": f"claim-{wake['wake_id']}",
            "status": "running",
            "cwd": f"/worker/{wake['task_id']}",
            "control": {"runner_kill": True},
            "metadata": {
                "wake_id": wake["wake_id"],
                "work_session_id": f"worksession-{wake['wake_id']}",
                "credential_admission_phase": "claim_bound",
                # Claim binding must not erase the supervisor's PTY state.
                "pty": False,
            },
        },
    }


try:
    os.environ.pop("PM_PERSONAL_AGENT_HOST_RECOVERY", None)
    os.environ.pop("PM_PERSONAL_AGENT_HOST_EXECUTION", None)
    agent_host._try = fake_try
    agent_host.heartbeat_capacity = lambda _inventory: {
        "active_sessions": len(launches),
        "local_auth": {"available": True},
    }
    agent_host.apply_authoritative_execution_policy = lambda *_args: False
    agent_host.handle_runner_controls = (
        lambda _inventory: control_polls.append(time.monotonic()) or [])
    agent_host.active_session_count = lambda _inventory: len(launches)
    agent_host.launch = fake_launch
    agent_host.confirm_started = lambda _record: True
    agent_host._register_preclaim_runner = lambda *_args: {"ok": True}
    agent_host.register_runner_session = lambda record, *_args: dict(record or {})
    agent_host.wait_for_runner_binding = fake_wait
    agent_host.supervisor_action = (
        lambda action, runner_id, payload=None: kills.append(
            (action, runner_id, payload)) or {"ok": True})

    started_at = time.monotonic()
    first = agent_host.run_once(inventory)
    elapsed = time.monotonic() - started_at
    ok(launches == [wake["wake_id"] for wake in wakes] and elapsed < 1,
       "all bind-required wakes launch in one poll without waiting for earlier binds")
    ok(len(first["acted"]) == 3
       and all(row.get("binding_pending") for row in first["acted"])
       and not completions,
       "launch receipts stay pending and do not acknowledge wakes before exact binding")

    second = agent_host.run_once(inventory)
    ok(len(heartbeats) >= 2 and len(control_polls) >= 2
       and second["pending"] == 0,
       "heartbeat and control polling continue while bind finalizers are waiting")

    release["wake-ui37-0"].set()
    release["wake-ui37-2"].set()
    deadline = time.monotonic() + 3
    while len(completions) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    ok(len(completions) == 2
       and {row["wake_id"] for row in completions}
       == {"wake-ui37-0", "wake-ui37-2"},
       "one successful and one failed finalizer complete independently")
    failed_result = next(row["result"] for row in completions
                         if row["wake_id"] == "wake-ui37-2")
    ok(failed_result["started"] is False
       and failed_result["reason"] == "runner_bind_timeout"
       and kills,
       "a bind timeout kills only its runner and completes fail-closed")

    release["wake-ui37-1"].set()
    deadline = time.monotonic() + 3
    while len(completions) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    final = agent_host.run_once(inventory)
    successful = [row["result"] for row in completions if row["result"]["started"]]
    ok(len(completions) == 3 and len(successful) == 2
       and all(row.get("claim_id") and row.get("work_session_id")
               and row.get("runner_registered") for row in successful),
       "each success completes with its own exact claim, Work Session, and Watch row")
    joined = agent_host._enrich_bound_runner_record(
        fake_launch(wakes[0], inventory, "run-transport-proof"),
        fake_wait(wakes[0], inventory, "run-transport-proof")["session"],
    )
    ok(joined["pty"] is True and "pty" not in joined["metadata"]
       and "stream_bind" not in joined
       and "stream_port" not in joined,
       "claim-bound preclaim placeholders cannot erase supervisor PTY transport")
    ok(len(final["acted"]) == 3
       and all(row.get("binding_pending") is False for row in final["acted"]),
       "the next nonblocking poll exposes every finalization receipt")
finally:
    for event in release.values():
        event.set()
    for name, value in originals.items():
        setattr(agent_host, name, value)


print(f"\nUI-37 parallel Agent Host binding: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
