#!/usr/bin/env python3
"""BUG-345: execution policy is advisory and cannot stop Autopilot launch."""
from __future__ import annotations

import inspect
import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="bug345-policy-free-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import coordinator_daemon  # noqa: E402
import store  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402
from switchboard.application.commands import execution_context  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.storage.repositories import project_execution_policy  # noqa: E402
from switchboard.storage.repositories import project_execution_readiness  # noqa: E402
from switchboard.storage.repositories import projects as projects_repo  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


BLOCKED = {
    "passed": False,
    "status": "blocked",
    "reason_code": "project_execution_policy_incomplete",
    "message": "policy is incomplete",
    "blockers": [{"code": "provider_connection_missing", "blocking": True}],
}


# Task Execution must dispatch even when an activated policy reports red.
store.init_project_registry()
store.init_db(P)
task = store.create_task(
    {"workstream_id": "BUG", "title": "Policy-free launch", "status": "Not Started"},
    actor="bug345-test", project=P,
)
saved_policy = project_execution_policy.get_project_execution_policy
saved_readiness = project_execution_readiness.get_project_execution_readiness
saved_enqueue = connect_dispatch.enqueue_task
dispatches = []
try:
    project_execution_policy.get_project_execution_policy = (
        lambda _project: {"configured": True, "activated": True})
    project_execution_readiness.get_project_execution_readiness = (
        lambda _project: dict(BLOCKED))
    connect_dispatch.enqueue_task = lambda *_args, **_kwargs: (
        dispatches.append("enqueued") or {
            "dispatched": True,
            "action": "started",
            "wake_id": "wake-bug345",
            "runner_session_id": "run-bug345",
        }
    )
    try:
        started = task_execution.start_task(
            task["task_id"], project=P, actor="bug345-test")
        refusal = {}
    except task_execution.TaskExecutionError as exc:
        started = {}
        refusal = exc.as_dict()
finally:
    project_execution_policy.get_project_execution_policy = saved_policy
    project_execution_readiness.get_project_execution_readiness = saved_readiness
    connect_dispatch.enqueue_task = saved_enqueue

ok(refusal == {} and dispatches == ["enqueued"],
   "activated red policy cannot stop Task Execution from reaching Connect")
ok(started.get("action") == "started",
   "Task Execution reports the policy-free dispatch")


# Connect must use only the canonical repository binding, even if an old policy
# is activated and its resolver is broken.
captured = []
resolve_calls = []
saved_resolve = execution_context.resolve
saved_request = connect_dispatch.coordination_repo.request_wake
saved_capacity = connect_dispatch.capacity_readback
saved_policy = project_execution_policy.get_project_execution_policy
saved_topology = projects_repo.get_project_repo_topology
try:
    project_execution_policy.get_project_execution_policy = (
        lambda _project: {"configured": True, "activated": True})

    def broken_resolver(**_kwargs):
        resolve_calls.append("called")
        raise execution_context.ExecutionContextError(
            "provider_connection_missing", "provider connection missing")

    execution_context.resolve = broken_resolver
    projects_repo.get_project_repo_topology = lambda _project: {
        "roles": {"canonical": {
            "configured": True,
            "repo": "6th-Element-Labs/projectplanner",
            "default_branch": "master",
        }}
    }
    connect_dispatch.coordination_repo.request_wake = (
        lambda **kwargs: captured.append(kwargs)
        or {"wake_id": "wake-connect-bug345", "status": "pending"})
    connect_dispatch.capacity_readback = lambda *_args, **_kwargs: {}
    connected = connect_dispatch.enqueue_task(
        {"task_id": "BUG-345", "_wsId": "BUG", "git_state": {}},
        project=P, actor="bug345-test", runtime="codex")
finally:
    execution_context.resolve = saved_resolve
    connect_dispatch.coordination_repo.request_wake = saved_request
    connect_dispatch.capacity_readback = saved_capacity
    project_execution_policy.get_project_execution_policy = saved_policy
    projects_repo.get_project_repo_topology = saved_topology

ok(connected.get("dispatched") is True and resolve_calls == [],
   "Connect never reads execution-policy context while launching")
wake_policy = captured[0]["policy"] if captured else {}
ok(wake_policy.get("repository_binding") == {
       "schema": "switchboard.repository_binding.v1",
       "project": P,
       "repo_role": "canonical",
       "repository": "6th-Element-Labs/projectplanner",
       "default_branch": "master",
   }, "Connect carries the canonical repository binding")
ok("execution_context" not in wake_policy
   and "placement" not in wake_policy
   and "scheduler" not in wake_policy,
   "Connect does not smuggle execution policy into the wake")


# Keep the source boundary obvious: no later refactor may reintroduce a launch
# gate under a new branch without failing this contract.
task_source = inspect.getsource(task_execution.start_task)
connect_source = inspect.getsource(connect_dispatch.enqueue_task)
tick_source = inspect.getsource(coordinator_daemon.CoordinatorDaemon.tick_project)
ok("get_project_execution_readiness" not in task_source
   and "get_project_execution_policy" not in task_source,
   "Task Execution contains no execution-policy launch gate")
ok("get_project_execution_policy" not in connect_source
   and "execution_context.resolve" not in connect_source,
   "Connect contains no execution-policy launch gate")
ok("_execution_readiness" not in tick_source
   and "_readiness_refusal" not in tick_source,
   "the coordinator contains no execution-readiness launch gate")

print(f"\nBUG-345 policy-free Autopilot: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
