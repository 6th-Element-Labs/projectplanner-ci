#!/usr/bin/env python3
"""WATCH-12: Start and execution reads expose honest Connect capacity."""
from __future__ import annotations

import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="watch12-capacity-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT  # noqa: E402,F401

import store  # noqa: E402
from execution_policy_fixture import (  # noqa: E402
    install_ready_execution_policy, ready_execution_context,
)
from switchboard.application.commands import connect_dispatch  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402

P = "switchboard"
connect_dispatch.execution_context.resolve = lambda **kwargs: ready_execution_context(
    kwargs["task_id"], runtime=kwargs["runtime"])
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def new_task(title):
    return store.create_task({"workstream_id": "WATCH", "title": title},
                             actor="watch12", project=P)["task_id"]


try:
    store.init_project_registry()
    store.init_db(P)
    install_ready_execution_policy(P)
    store.register_host({
        "host_id": "host/full", "display_name": "Full Mac", "status": "online",
        "runtimes": [{"runtime": "codex", "available": True,
                      "local_auth": {"available": True}}],
        "limits": {"max_sessions": 8},
        "capacity": {"active_sessions": 8},
    }, principal_id="watch12", project=P)

    first = new_task("first pending")
    first_start = task_execution.start_task(first, project=P, actor="watch12")
    second = new_task("second pending")
    second_start = task_execution.start_task(second, project=P, actor="watch12")
    capacity = second_start.get("capacity") or {}
    ok(second_start.get("started") is True, "Start enqueues through Connect")
    ok(capacity.get("queue_position") == 2 and capacity.get("pending_ahead") == 1,
       f"Start reports pending wake position, got {capacity!r}")
    hosts = capacity.get("matching_online_hosts") or []
    ok(hosts and hosts[0].get("host_id") == "host/full"
       and hosts[0].get("active_sessions") == 8
       and hosts[0].get("max_sessions") == 8,
       f"Start reports matching host active/max sessions, got {hosts!r}")

    execution = task_execution.get_task_execution(second, project=P)
    panel = execution.get("panel") or {}
    panel_capacity = panel.get("capacity") or {}
    ok(panel.get("state") == "queued" and panel.get("behind_active_runs") == 8,
       f"execution projection says queued behind eight, got {panel!r}")
    ok(panel_capacity.get("queue_position") == 2,
       "execution projection preserves the capacity readback")

    no_host = new_task("no matching host")
    no_host_start = task_execution.start_task(
        no_host, project=P, actor="watch12", runtime="claude")
    reason = ((no_host_start.get("capacity") or {}).get("no_capacity") or {}).get("reason")
    ok(reason == "no_matching_online_hosts",
       f"zero matching hosts has explicit no-capacity reason, got {reason!r}")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
