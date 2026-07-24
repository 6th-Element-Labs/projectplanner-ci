#!/usr/bin/env python3
"""BUG-133: resume after a COMPLETED Connect wake must mint a fresh generation.

The WATCH-7 incident: a Connect reviewer ran and exited; its wake closed as
``completed`` (not failed, so the BUG-130 killed-runner fix never engaged).
The operator's Resume review re-entered ``enqueue_task`` with an empty
predecessor, reusing the ``...:initial`` idempotency key -- while ordinary
board edits had moved ``task.updated_at``, which is embedded in the idem
request payload via ``assignment.queued_at``. Same key + different hash ->
``db/core._idem_hit`` returned a raw "idempotency conflict" to the panel and
no replacement runner ever started.

Contract pinned here: when the latest Connect wake for a task is terminal
(completed OR failed OR cancelled), a new start advances the idempotency
generation past it -- even when the caller supplies no predecessor and the
task row has been edited since the original dispatch.
"""
import os
import shutil
import sys
import tempfile
import dataclasses

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="bug133-resume-completed-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

# Keep runnable on the macOS system Python 3.9 used by the native host.
if sys.version_info < (3, 10):
    _dataclass = dataclasses.dataclass

    def _compat_dataclass(*args, **kwargs):
        kwargs.pop("slots", None)
        return _dataclass(*args, **kwargs)

    dataclasses.dataclass = _compat_dataclass

import store  # noqa: E402
from execution_policy_fixture import install_ready_execution_policy  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402

P = "switchboard"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(P)
    install_ready_execution_policy(P)
    task = store.create_task({"workstream_id": "BUG", "title": "BUG-133 regression"},
                             actor="bug133-test", project=P)
    task_id = task["task_id"]

    # First start: the original generation, exactly as connect_dispatch mints it
    # (idem payload embeds assignment.queued_at derived from task.updated_at).
    first = task_execution.start_task(task_id, project=P, actor="bug133-test")
    assert first["started"] is True, first
    first_wake = first["wake_id"]

    runner_id = "run_bug133"
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": "host/bug133",
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
            "wake_id": first_wake,
            "pty": True,
            "stream_bind": "127.0.0.1",
            "stream_port": 43133,
        },
    }, actor="bug133-test", project=P)
    completed = store.complete_wake(
        first_wake, result={"started": True, "runner_session_id": runner_id},
        runner_session_id=runner_id, agent_id=f"agent/codex/{task_id.lower()}",
        actor="bug133-test", project=P)
    ok(completed["status"] == "completed", "first Connect wake closes as completed")

    # The reviewer finishes and exits -- an ordinary successful run, no kill.
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": "host/bug133",
        "agent_id": f"agent/codex/{task_id.lower()}",
        "runtime": "codex",
        "task_id": task_id,
        "status": "exited",
        "metadata": {"wake_id": first_wake},
    }, actor="bug133-test", project=P)

    # Ordinary board edits move task.updated_at (the WATCH-7 trigger).
    store.update_task(task_id, {"description": "edited after the run completed"},
                      actor="bug133-test", project=P)

    # Resume: must start a fresh generation, not die on "idempotency conflict".
    resumed = task_execution.start_task(task_id, project=P, actor="bug133-test")
    ok(resumed.get("started") is True and resumed.get("action") == "started",
       f"resume after a completed wake starts a replacement "
       f"(got action={resumed.get('action')}, error={resumed.get('start_error')})")
    ok(resumed.get("wake_id") and resumed["wake_id"] != first_wake,
       "the replacement rides a NEW wake generation, not a replay of the old one")

    wakes = store.list_wake_intents(task_id=task_id, project=P)
    ok(len(wakes) == 2, f"exactly two wakes exist after the resume ({len(wakes)})")
    ok(any(w["wake_id"] == resumed["wake_id"] and w["status"] == "pending"
           for w in wakes),
       "the new generation is pending for a host to claim")
    new_wake = next(w for w in wakes if w["wake_id"] == resumed["wake_id"])
    ok(str(first_wake) in str(new_wake.get("idem_key") or ""),
       "the new idempotency key is chained to the completed predecessor wake")

    # A second resume click while the new wake is in flight attaches to it
    # idempotently -- it must not fork a third generation or conflict.
    again = task_execution.start_task(task_id, project=P, actor="bug133-test")
    ok(again.get("action") in {"starting", "started"}
       and again.get("wake_id") == resumed["wake_id"],
       f"double-click resume is idempotent (got action={again.get('action')}, "
       f"wake={again.get('wake_id')})")
    ok(len(store.list_wake_intents(task_id=task_id, project=P)) == 2,
       "no third generation is forked by the repeat click")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nBUG-133 resume-after-completed-wake: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
