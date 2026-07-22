#!/usr/bin/env python3
"""WATCH-5: get_task_execution exposes four honest panel states.

Queued (wake pending) / Starting (wake claimed) / Live (host attached) /
Detached (runner live, bridge gone). The browser must not collapse pending and
claimed into one lying "Starting…" spinner.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="watch5-states-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT  # noqa: E402,F401

import store  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.application import runner_pty_relay as relay  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def new_task(title):
    return store.create_task({"workstream_id": "WATCH", "title": title},
                             actor="watch5", project=P)["task_id"]


def pending_wake(task_id):
    return store.request_wake(
        {"runtime": "codex", "agent_id": f"codex/{task_id}", "task_id": task_id},
        reason="watch5 queue fixture", source="watch5",
        task_id=task_id, actor="watch5", project=P,
        idem_key=f"watch5-{task_id}-initial",
        policy={"mode": "connect"},
    )


def register_host(host_id="host/mac", active=8, max_sessions=8):
    return store.register_host({
        "host_id": host_id,
        "display_name": "Steve Mac",
        "status": "online",
        "runtimes": [{"runtime": "codex", "available": True,
                      "local_auth": {"available": True}}],
        "limits": {"max_sessions": max_sessions},
        "capacity": {"active_sessions": active},
    }, principal_id="watch5", project=P)


def runner(task_id, runner_id, status="running"):
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": "host/mac",
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "claim_id": f"claim-{runner_id}", "status": status,
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3",
                    "runner_open": True, "runner_inject": True},
        "metadata": {"wake_id": f"wake-{runner_id}",
                     "work_session_id": f"ws-{runner_id}"},
        "heartbeat_at": time.time(), "heartbeat_ttl_s": 180,
    }, actor="watch5", project=P)


try:
    store.init_project_registry()
    store.init_db(P)
    register_host(active=8, max_sessions=8)

    # 1) Pending wake + saturated host → Queued with behind-N detail.
    queued_task = new_task("queued behind capacity")
    wake = pending_wake(queued_task)
    ok(wake["status"] == "pending", "fixture wake is pending")
    v = task_execution.get_task_execution(queued_task, project=P)
    panel = v.get("panel") or {}
    ok(panel.get("state") == "queued", f"pending wake is panel.state=queued, got {panel!r}")
    ok(v.get("starting") is True, "queued keeps starting=true for back-compat")
    ok(v.get("running") is False, "queued is not running")
    ok(panel.get("behind_active_runs") == 8, f"behind_active_runs=8, got {panel!r}")
    ok(panel.get("host_id") == "host/mac", f"queue names the saturated host, got {panel!r}")
    detail = str(panel.get("detail") or "")
    ok("behind 8" in detail.lower() and "host/mac" in detail,
       f"queued detail names behind-N and host, got {detail!r}")

    # 2) Claimed wake, no runner yet → Starting.
    register_host(host_id="host/spare", active=1, max_sessions=8)
    starting_task = new_task("wake claimed, registering")
    wake2 = pending_wake(starting_task)
    claimed = store.claim_wake("host/spare", wake2["wake_id"],
                               actor="host/spare", project=P)
    wakes = store.list_wake_intents(task_id=starting_task, project=P)
    ok(any(w["status"] == "claimed" for w in wakes),
       f"wake claimed fixture ok: {claimed!r} wakes={wakes!r}")
    v = task_execution.get_task_execution(starting_task, project=P)
    panel = v.get("panel") or {}
    ok(panel.get("state") == "starting", f"claimed wake is starting, got {panel!r}")
    ok(v.get("starting") is True and v.get("running") is False,
       "starting keeps starting=true / running=false")

    # 3) Live runner + host_attached → Live.
    live_task = new_task("host attached live")
    runner(live_task, "run_live")
    _orig_attach = relay.host_attached_for
    relay.host_attached_for = lambda sid: True if sid == "run_live" else _orig_attach(sid)
    v = task_execution.get_task_execution(live_task, project=P)
    panel = v.get("panel") or {}
    ok(panel.get("state") == "live", f"attached runner is live, got {panel!r}")
    ok(v.get("running") is True, "live keeps running=true")
    ok(panel.get("host_attached") is True, f"live panel reports host_attached, got {panel!r}")

    # 4) Live runner + host_attached false → Detached.
    dark_task = new_task("bridge gone")
    runner(dark_task, "run_dark")
    relay.host_attached_for = lambda sid: (
        False if sid == "run_dark" else (True if sid == "run_live" else _orig_attach(sid)))
    v = task_execution.get_task_execution(dark_task, project=P)
    panel = v.get("panel") or {}
    ok(panel.get("state") == "detached", f"unattached live runner is detached, got {panel!r}")
    ok(v.get("running") is True, "detached still has a running process")
    ok(panel.get("host_attached") is False, f"detached reports host_attached=false, got {panel!r}")
    ok("reconnect" in str(panel.get("detail") or "").lower()
       or "detached" in str(panel.get("label") or "").lower()
       or "bridge" in str(panel.get("detail") or "").lower(),
       f"detached copy mentions reconnect/bridge, got {panel!r}")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
