#!/usr/bin/env python3
"""BUG-279: a verified host kill closes the exact late-bound claim/session."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug279-terminal-kill-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)

import store  # noqa: E402
from db.connection import _conn  # noqa: E402


P = "switchboard"
AGENT = "agent/codex/bug279"
RUNNER = "run_bug279"

try:
    store.init_db(P)
    task = store.create_task({
        "workstream_id": "BUG",
        "title": "terminal control cleanup proof",
        "status": "Not Started",
        "ui_impact": "no",
    }, actor="bug279-test", project=P)
    task_id = task["task_id"]
    wake = store.request_wake(
        {"runtime": "codex", "task_id": task_id}, task_id=task_id,
        policy={"mode": "connect"}, reason="Connect assignment",
        idem_key=f"bug279:{task_id}", actor="bug279-test", project=P,
    )
    store.upsert_runner_session({
        "runner_session_id": RUNNER,
        "host_id": "host/bug279",
        "agent_id": AGENT,
        "runtime": "codex",
        "task_id": task_id,
        "status": "running",
        "control": {"tier": "T3", "managed_process": True, "runner_kill": True},
        # This is the production race: the host registered before the CLI made
        # its claim/Work Session, so neither late binding is copied here.
        "metadata": {
            "wake_id": wake["wake_id"],
            "native_host_execution": True,
            "execution_id": "execlease-bug279",
            "execution_generation": 1,
            "execution_role": "implementation",
            "lease_epoch": 1,
        },
    }, actor="bug279-test", project=P)
    store.complete_wake(
        wake["wake_id"], result={"started": True, "runner_session_id": RUNNER},
        runner_session_id=RUNNER, agent_id=AGENT,
        actor="bug279-test", project=P,
    )
    work_session = store.create_work_session({
        "agent_id": AGENT,
        "task_id": task_id,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-proof",
        "upstream": "origin/master",
        "base_sha": "a" * 40,
        "head_sha": "a" * 40,
        "storage_mode": "worktree",
        "worktree_path": str(TMP),
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="bug279-test", project=P)["work_session"]
    claim = store.claim_task(
        task_id, AGENT, actor="bug279-test", project=P,
        work_session_id=work_session["work_session_id"],
        session_policy_profile="code_strict", require_work_session=True,
    )
    assert claim.get("claimed") is True, claim
    with _conn(P) as c:
        c.execute(
            "UPDATE task_claims SET runner_session_id=?, execution_generation=1, "
            "execution_role='implementation', lease_epoch=1 WHERE id=?",
            (RUNNER, claim["claim_id"]),
        )
        c.execute(
            "UPDATE work_sessions SET runner_session_id=?, execution_generation=1, "
            "execution_role='implementation', lease_epoch=1 WHERE work_session_id=?",
            (RUNNER, work_session["work_session_id"]),
        )

    kill = store.request_runner_control(
        RUNNER, "kill", reason="fail-closed acceptance stop",
        actor="bug279-test", project=P,
    )
    assert kill.get("requested") is True, kill
    finished = store.complete_runner_control_request(
        kill["request_id"], result={"status": "killed", "alive": False},
        status="completed", actor="host/bug279", project=P,
    )
    assert finished.get("status") == "completed", finished

    with _conn(P) as c:
        claim_row = c.execute(
            "SELECT status,abandon_reason FROM task_claims WHERE id=?",
            (claim["claim_id"],),
        ).fetchone()
    assert claim_row["status"] == "abandoned", dict(claim_row)
    assert claim_row["abandon_reason"] == f"terminal_runner:{RUNNER}:killed", dict(claim_row)
    closed = store.get_work_session(work_session["work_session_id"], project=P)
    assert closed["status"] == "expired", closed
    current = store.get_task(task_id, project=P)
    assert current["status"] == "Not Started" and not current.get("assignee"), current
    runner = store.get_runner_session(RUNNER, project=P)
    assert not runner.get("claim_id"), runner
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("PASS: verified host kill releases the exact late-bound claim")
print("PASS: verified host kill expires only its exact claim-bound Work Session")
