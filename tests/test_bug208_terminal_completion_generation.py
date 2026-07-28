#!/usr/bin/env python3
"""BUG-208: a terminal completion runner advances the Connect generation."""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="bug208-terminal-completion-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

if sys.version_info < (3, 10):
    _dataclass = dataclasses.dataclass

    def _compat_dataclass(*args, **kwargs):
        kwargs.pop("slots", None)
        return _dataclass(*args, **kwargs)

    dataclasses.dataclass = _compat_dataclass

import store  # noqa: E402
from execution_readiness_fixture import configure_ready_project  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402

P = "switchboard"
HEAD = "c" * 40
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def start(task_id: str, waited_seconds: float):
    return task_execution.start_task(
        task_id,
        project=P,
        actor="bug208-test",
        role="remediation",
        source_sha=HEAD,
        reason_code="required_exact_head_ci_failed",
        route="remediation",
        decision_attempt=1,
        state_version=1,
        findings=[{
            "code": "review_stalled_no_verdict",
            "blocking": True,
            "waited_seconds": waited_seconds,
        }],
    )


try:
    store.init_db(P)
    configure_ready_project(P, actor="bug208-test")
    task = store.create_task(
        {"workstream_id": "BUG", "title": "BUG-208 regression"},
        actor="bug208-test",
        project=P,
    )
    task_id = task["task_id"]

    first = start(task_id, 1800.0)
    assert first["started"] is True, first
    first_wake = first["wake_id"]
    runner_id = "run_bug208_old"
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": "host/bug208",
        "agent_id": f"agent/codex/{task_id.lower()}",
        "runtime": "codex",
        "task_id": task_id,
        "status": "running",
        "control": {"tier": "T3", "managed_process": True},
        "metadata": {"wake_id": first_wake},
    }, actor="bug208-test", project=P)
    store.complete_wake(
        first_wake,
        result={"started": True, "runner_session_id": runner_id},
        runner_session_id=runner_id,
        agent_id=f"agent/codex/{task_id.lower()}",
        actor="bug208-test",
        project=P,
    )
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": "host/bug208",
        "agent_id": f"agent/codex/{task_id.lower()}",
        "runtime": "codex",
        "task_id": task_id,
        "status": "exited",
        "metadata": {"wake_id": first_wake},
    }, actor="bug208-test", project=P)

    replacement = start(task_id, 3600.0)
    ok(replacement.get("started") is True,
       f"terminal completion runner permits a fresh start ({replacement})")
    ok(replacement.get("wake_id") != first_wake,
       "fresh remediation uses a new wake")

    wakes = store.list_wake_intents(task_id=task_id, project=P)
    ok(len(wakes) == 2, f"exactly two wake generations exist ({len(wakes)})")
    old_wake = next(w for w in wakes if w["wake_id"] == first_wake)
    old_execution = str(
        (old_wake.get("policy") or {}).get("lifecycle", {}).get("execution_id") or ""
    )
    new_wake = next(w for w in wakes if w["wake_id"] == replacement["wake_id"])
    ok(old_execution and old_execution in str(new_wake.get("idem_key") or ""),
       "Task Execution chains the successor past the terminal execution")
    ok((new_wake.get("policy") or {}).get("lifecycle", {}).get(
        "acceptance_findings", [{}])[0].get("waited_seconds") == 3600.0,
       "fresh assignment retains the full current evidence dossier")

    replay = start(task_id, 5400.0)
    ok(replay.get("action") in {"starting", "started"}
       and replay.get("wake_id") == replacement.get("wake_id"),
       "replay while replacement is pending reuses the same wake")
    ok(len(store.list_wake_intents(task_id=task_id, project=P)) == 2,
       "replay does not fork a duplicate execution")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nBUG-208 terminal completion generation: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
