#!/usr/bin/env python3
"""BREAKDOWN 25: the execution principal is never the caller's identity.

connect_dispatch used to set ``assignment.principal_ref`` to ``caller_agent_id``
when one was supplied. The worker's boot note then instructed
``register_agent(agent_id=<principal_ref>, task_id=<its task>)``, which
re-bound the CALLER'S shared presence row to the worker's task — and the
caller's next ``start_task`` for any other task refused with
``agent_registered_on_different_task``. The Autopilot wedged itself exactly
this way (2026-07-25): its coordinator identity got task-bound to COORD-48 by
the worker it launched, and every ``start_remediation`` retry for ENFORCE-14
was refused. Six retries, zero runners, conflict PR parked.

Pins: the principal is always the minted per-task worker identity; the caller
identity may appear only as wake authorization, and a coordinator can start
two different tasks back-to-back under one registration.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = Path(tempfile.mkdtemp(prefix="breakdown25-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402
from tests.execution_policy_fixture import (  # noqa: E402
    install_ready_execution_policy,
    ready_execution_context,
)

P = "switchboard"
COORDINATOR = "switchboard/coordinator-autopilot/breakdown25"
passed = failed = 0


def ok(condition, label):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}")


store.init_db(P)
install_ready_execution_policy(P)
connect_dispatch.execution_context.resolve = lambda **kwargs: ready_execution_context(
    kwargs["task_id"], runtime=kwargs["runtime"])
first = store.create_task(
    {"workstream_id": "COORD", "title": "breakdown25 first task"},
    actor="breakdown25", project=P)
second = store.create_task(
    {"workstream_id": "ENFORCE", "title": "breakdown25 second task"},
    actor="breakdown25", project=P)

# One coordinator identity, registered launcher-style (no task binding) —
# exactly how coordinator_daemon and scoped_completion_coordinator register.
store.register_agent(
    COORDINATOR, runtime="scoped-completion-coordinator", lane="COORD",
    ttl_s=600, actor=COORDINATOR, project=P)

results = {}
for task in (first, second):
    results[task["task_id"]] = connect_dispatch.enqueue_task(
        task, project=P, actor=COORDINATOR, runtime="codex",
        caller_agent_id=COORDINATOR)

for task_id, result in results.items():
    ok(result.get("dispatched") is True,
       f"coordinator start of {task_id} dispatches "
       f"(got {result.get('error') or result.get('reason')})")

wakes = {w["task_id"]: w for w in store.list_wake_intents(project=P)}
for task_id in results:
    assignment = ((wakes.get(task_id) or {}).get("policy") or {}).get(
        "assignment") or {}
    principal = str(assignment.get("principal_ref") or "")
    ok(principal == f"agent/codex/{task_id.lower()}",
       f"{task_id} worker principal is the minted per-task identity "
       f"(got {principal!r})")
    ok(principal != COORDINATOR,
       f"{task_id} worker principal is never the caller's identity")
    selector = (wakes.get(task_id) or {}).get("selector") or {}
    ok(str(selector.get("agent_id") or "") == principal,
       f"{task_id} wake selector carries the worker principal, not the caller")

# The regression itself: after a worker registers the first task's principal
# task-bound (as its boot note instructs), the coordinator's registration is
# untouched and a start for the SECOND task still binds.
store.register_agent(
    f"agent/codex/{first['task_id'].lower()}", runtime="codex", lane="COORD",
    task_id=first["task_id"], ttl_s=600,
    actor=f"agent/codex/{first['task_id'].lower()}", project=P)
from switchboard.storage.repositories import access as access_repo  # noqa: E402
binding = access_repo.resolve_write_actor(
    "env-mcp-token", project=P, task_id=second["task_id"],
    agent_id=COORDINATOR)
ok(binding.get("ok") is True
   and binding.get("error") != "agent_registered_on_different_task",
   "coordinator identity still binds for a second task after a worker "
   f"registered task-bound (got {binding.get('error')})")

print(f"\nBREAKDOWN 25 execution principal split: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
