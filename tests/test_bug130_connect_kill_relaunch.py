#!/usr/bin/env python3
"""BUG-130: Connect runners stay watchable and killed wakes cannot be replayed."""
import os
import shutil
import sys
import tempfile
import dataclasses

from path_setup import ROOT

_TMP = tempfile.mkdtemp(prefix="bug130-connect-kill-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

# The macOS system Python used by the native host is 3.9; production/CI is
# newer. Keep this standalone regression runnable on the host as well.
if sys.version_info < (3, 10):
    _dataclass = dataclasses.dataclass

    def _compat_dataclass(*args, **kwargs):
        kwargs.pop("slots", None)
        return _dataclass(*args, **kwargs)

    dataclasses.dataclass = _compat_dataclass

import store  # noqa: E402
from execution_policy_fixture import (  # noqa: E402
    install_ready_execution_policy, ready_execution_context,
)
from switchboard.application.commands import connect_dispatch  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402

P = "switchboard"
connect_dispatch.execution_context.resolve = lambda **kwargs: ready_execution_context(
    kwargs["task_id"], runtime=kwargs["runtime"])

try:
    store.init_db(P)
    install_ready_execution_policy(P)
    task = store.create_task({"workstream_id": "BUG", "title": "BUG-130 regression"},
                             actor="bug130-test", project=P)
    task_id = task["task_id"]
    wake = store.request_wake(
        {"runtime": "codex", "task_id": task_id}, task_id=task_id,
        policy={"mode": "connect"}, reason="Connect assignment",
        idem_key=f"connect-start:v1:{P}:{task_id}:codex:initial",
        actor="bug130-test", project=P)
    runner_id = "run_bug130"
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": "host/bug130",
        "agent_id": f"agent/codex/{task_id.lower()}",
        "runtime": "codex",
        "task_id": task_id,
        "status": "running",
        "control": {"tier": "T3", "managed_process": True,
                    "runner_kill": True, "runner_open": True},
        "metadata": {
            "connect_assignment": True,
            "assignment_schema": "switchboard.connect.assignment.v1",
            "native_host_execution": True,
            "wake_id": wake["wake_id"],
            "pty": True,
            "stream_bind": "127.0.0.1",
            "stream_port": 43130,
        },
    }, actor="bug130-test", project=P)
    completed = store.complete_wake(
        wake["wake_id"], result={"started": True, "runner_session_id": runner_id},
        runner_session_id=runner_id, agent_id=f"agent/codex/{task_id.lower()}",
        actor="bug130-test", project=P)
    assert completed["status"] == "completed", completed

    watch = store.resolve_runner_watch(task_id, include_stale=True, project=P)
    assert watch["watchable"] is True, watch
    assert watch["binding_mode"] == "native_assignment", watch

    kill = store.request_runner_control(
        runner_id, "kill", reason="operator killed Connect run",
        actor="bug130-test", project=P)
    assert kill["requested"] is True, kill
    finished = store.complete_runner_control_request(
        kill["request_id"], result={"status": "killed", "alive": False},
        status="completed", actor="bug130-host", project=P)
    assert finished["status"] == "completed", finished

    old = next(row for row in store.list_wake_intents(task_id=task_id, project=P)
               if row["wake_id"] == wake["wake_id"])
    assert old["status"] == "failed", old
    assert old["result"]["failure_class"] == "runner_killed", old

    relaunched = task_execution.start_task(task_id, project=P, actor="bug130-test")
    assert relaunched["action"] == "started", relaunched
    assert relaunched["started"] is True, relaunched
    assert relaunched["wake_id"] != wake["wake_id"], relaunched
    wakes = store.list_wake_intents(task_id=task_id, project=P)
    assert len(wakes) == 2, wakes
    assert any(row["status"] == "pending" and row["wake_id"] == relaunched["wake_id"]
               for row in wakes), wakes
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("PASS: Connect runner is watchable before claim/work-session binding")
print("PASS: killing a Connect runner fails its wake and relaunch mints a fresh generation")
