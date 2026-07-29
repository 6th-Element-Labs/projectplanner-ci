#!/usr/bin/env python3
"""BUG-227: Capacity startup stays live, bounded, reserved, and project-fair."""
from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401
from adapters import agent_host
from adapters import repository_workspace


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


inventory = {
    "host_id": "host/bug227",
    "repo_root": "/tmp/bug227",
    "limits": {"max_sessions": 2},
    "policy": {"allow_work": True},
    "capacity": {"placement": {"projects": ["atlas", "switchboard"]}},
    "runtimes": [{"runtime": "codex"}],
}
wake = {"wake_id": "wake-bug227", "_host_project": "switchboard"}
heartbeats = []
observed_capacity = []
runner_renewals = []
control_polls = []


def slow_materialize(*, timeout_s, **_kwargs):
    observed_capacity.append(agent_host.heartbeat_capacity(inventory))
    time.sleep(timeout_s)
    raise repository_workspace.WorkspaceMaterializationError(
        "workspace_materialize_timeout",
        "bounded launch expired")


with patch.object(agent_host, "_try",
                  side_effect=lambda method, path, body=None:
                  heartbeats.append(dict(body or {})) or {"ok": True}), \
        patch.object(agent_host, "active_session_count", return_value=0), \
        patch.object(agent_host, "renew_live_direct_runners",
                     side_effect=lambda _inventory:
                     runner_renewals.append(time.monotonic()) or []), \
        patch.object(agent_host, "handle_runner_controls",
                     side_effect=lambda _inventory:
                     control_polls.append(time.monotonic()) or []), \
        patch.dict(agent_host.os.environ, {
            "PM_CONNECT_MATERIALIZE_TIMEOUT_SECONDS": "0.18",
            "PM_CONNECT_CLAIM_HOLD_SECONDS": "0.5",
        }):
    started = time.monotonic()
    try:
        agent_host._materialize_for_launch(
            slow_materialize, {}, wake, inventory)
    except repository_workspace.WorkspaceMaterializationError as exc:
        elapsed = time.monotonic() - started
        ok(exc.code == "workspace_materialize_timeout" and elapsed < 0.5,
           "materialization finishes inside the claimed-wake hold")
    else:
        ok(False, "bounded materialization must time out")

ok(bool(heartbeats),
   "host heartbeat continues while repository materialization is blocked")
ok(bool(runner_renewals) and bool(control_polls),
   "runner lease renewal and explicit control polling continue during launch")
ok(observed_capacity
   and observed_capacity[0]["reserved_launches"] == 1
   and observed_capacity[0]["occupied_sessions"] == 1
   and observed_capacity[0]["headroom"] == 1,
   "an in-flight launch reserves one advertised capacity slot")
ok(agent_host.reserved_launch_count() == 0,
   "the launch reservation is released after failure")
with patch.dict(agent_host.os.environ, {
    "PM_CONNECT_MATERIALIZE_TIMEOUT_SECONDS": "120",
    "PM_CONNECT_CLAIM_HOLD_SECONDS": "90",
}):
    ok(agent_host._bounded_materialization_timeout() <= 81,
       "configured launch timeout is capped below the claimed-wake hold")

ordered = agent_host._fair_wake_order([
    {"wake_id": "atlas-1", "_host_project": "atlas"},
    {"wake_id": "atlas-2", "_host_project": "atlas"},
    {"wake_id": "switchboard-1", "_host_project": "switchboard"},
    {"wake_id": "switchboard-2", "_host_project": "switchboard"},
], ["atlas", "switchboard"])
ok([row["wake_id"] for row in ordered] == [
    "switchboard-1", "atlas-1", "switchboard-2", "atlas-2"],
   "project queues are interleaved with the primary project first")

with tempfile.TemporaryDirectory(prefix="bug227-lock-") as temporary:
    lock_path = Path(temporary) / "cache.lock"
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with repository_workspace._locked(lock_path):
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=holder)
    thread.start()
    entered.wait(timeout=1)
    try:
        with repository_workspace._locked(
                lock_path, deadline=time.monotonic() + 0.08):
            ok(False, "contended lock must respect the shared deadline")
    except repository_workspace.WorkspaceMaterializationError as exc:
        ok(exc.code == "workspace_materialize_timeout",
           "cache-lock wait uses the same materialization deadline")
    finally:
        release.set()
        thread.join(timeout=1)
    with repository_workspace._locked(
            lock_path, deadline=time.monotonic() + 0.2):
        ok(True, "timed-out lock waiter leaves no orphan lock holder")

with patch.object(repository_workspace.subprocess, "run",
                  side_effect=subprocess.TimeoutExpired(["git", "fetch"], 0.1)):
    try:
        repository_workspace._run(
            ["git", "fetch"], deadline=time.monotonic() + 0.1)
    except repository_workspace.WorkspaceMaterializationError as exc:
        ok(exc.code == "workspace_materialize_timeout",
           "a timed-out git child becomes one typed bounded-launch failure")
    else:
        ok(False, "timed-out git child must fail closed")

with tempfile.TemporaryDirectory(prefix="bug227-receipts-") as temporary, \
        patch.dict(agent_host.os.environ, {
            "PM_RUNNER_DIR": temporary,
            "PM_PENDING_RECEIPT_REPLAY_LIMIT": "1",
        }):
    agent_host._persist_pending_wake_receipt({
        "project": "switchboard",
        "wake_id": "wake-stale",
        "result": {"task_id": "BUG-227"},
    })
    agent_host._persist_pending_wake_receipt({
        "project": "switchboard",
        "wake_id": "wake-waiting",
        "result": {"task_id": "BUG-227"},
    })
    with patch.object(agent_host, "_require", return_value={
        "error": "wake_not_found",
        "error_code": "wake_not_found",
        "failure_class": "failed_gate",
        "http_status": 404,
    }):
        wake_outcomes = agent_host._drain_pending_wake_receipts()
    pending_wakes = list(
        agent_host._pending_wake_receipt_dir().glob("*.json"))
    archived = list(
        (Path(temporary) / "_terminal_receipt_archive").glob("*.json"))
    ok(
        len(wake_outcomes) == 1
        and wake_outcomes[0]["archived"]
        and wake_outcomes[0]["error"] == "http_404:wake_not_found"
        and len(pending_wakes) == 1
        and len(archived) == 1,
        "wake receipt replay is bounded and irrecoverable 4xx is archived")

    agent_host._persist_pending_stop_receipt({
        "host_id": "host/bug227",
        "runner_session_id": "runner-transient",
        "task_id": "BUG-227",
    })
    with patch.object(agent_host, "_require", return_value={
        "error": "control_plane_unavailable",
        "error_code": "control_plane_unavailable",
        "failure_class": "broken_connection",
        "http_status": 503,
    }):
        stop_outcomes = agent_host._drain_pending_stop_receipts(
            "host/bug227")
    ok(
        len(stop_outcomes) == 1
        and not stop_outcomes[0]["archived"]
        and stop_outcomes[0]["error"]
        == "http_503:control_plane_unavailable"
        and len(list(
            agent_host._pending_stop_receipt_dir().glob("*.json"))) == 1,
        "transient runner-stop receipt remains queued with safe HTTP evidence")

print(f"\nBUG-227 Capacity launch isolation: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
