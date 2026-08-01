#!/usr/bin/env python3
"""BUG-270: a server-fenced runner remains visible until Host terminal ack."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug270-terminal-surrender-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_RUNNER_DIR"] = str(TMP / "runner")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from adapters import agent_host  # noqa: E402
from db.connection import _conn  # noqa: E402


P = "switchboard"
HOST_ID = "host/bug270"
RUNNER_ID = "run_bug270_review"
TASK_ID = "BUG-270"


def surrendered_runner(*, claim_id="", work_session_id=""):
    return {
        "runner_session_id": RUNNER_ID,
        "host_id": HOST_ID,
        "agent_id": "agent/codex/bug270",
        "runtime": "codex",
        "task_id": TASK_ID,
        "claim_id": claim_id,
        "status": "running",
        "heartbeat_ttl_s": 180,
        "metadata": {
            "native_host_execution": True,
            "connect_assignment": True,
            "execution_id": "exec-bug270",
            "execution_generation": 1,
            "execution_role": "review_merge",
            "lease_epoch": 2,
            "work_session_id": work_session_id,
            "lease_surrender": {
                "schema": "switchboard.runner_lease_surrender.v1",
                "authority": "terminal_task",
                "execution_id": "exec-bug270",
                "generation": 1,
                "lease_epoch": 2,
            },
        },
    }


saved_run = agent_host.subprocess.run
saved_try = agent_host._try
saved_persist = agent_host._persist_pending_stop_receipt
saved_delete = agent_host._delete_pending_stop_receipt
posted = []
persisted = []
deleted = []


def supervisor_list(*_args, **_kwargs):
    # Local Capacity truth has already observed the process exit.  The local
    # terminal word is not proof that the central receipt was acknowledged.
    return SimpleNamespace(returncode=0, stdout=json.dumps({"sessions": [{
        "runner_session_id": RUNNER_ID,
        "host_id": HOST_ID,
        "agent_id": "agent/codex/bug270",
        "runtime": "codex",
        "task_id": TASK_ID,
        "status": "exited",
        "alive": False,
        "pid": 270,
        "metadata": {},
    }]}), stderr="")


def fake_try(method, path, body=None):
    if method == "GET":
        assert "pending_completion=true" in path, path
        return {"sessions": store.list_runner_sessions(
            host_id=HOST_ID,
            include_stale=True,
            pending_completion=True,
            project=P,
        )}
    posted.append((method, path, dict(body or {})))
    if path == agent_host.P_HEARTBEAT_RUNNER:
        return store.upsert_runner_session(
            dict(body or {}), actor=HOST_ID, project=P)
    return {"ok": True}


try:
    store.init_db(P)
    task = store.create_task({
        "workstream_id": "BUG", "title": "already-exited surrender proof",
        "status": "Not Started", "ui_impact": "no",
    }, actor="bug270-test", project=P)
    TASK_ID = task["task_id"]
    work_session = store.create_work_session({
        "agent_id": "agent/codex/bug270", "task_id": TASK_ID,
        "runtime": "codex", "repo_role": "canonical",
        "branch": f"codex/{TASK_ID}-proof", "upstream": "origin/master",
        "base_sha": "a" * 40, "head_sha": "a" * 40,
        "storage_mode": "worktree", "worktree_path": str(TMP),
        "status": "active", "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {
            "ok": True, "verdict": "pass", "findings": [],
        }},
    }, actor="bug270-test", project=P)["work_session"]
    claim = store.claim_task(
        TASK_ID, "agent/codex/bug270", principal_id="principal/bug270",
        actor="bug270-test", project=P,
        work_session_id=work_session["work_session_id"],
        session_policy_profile="code_strict", require_work_session=True,
    )
    assert claim.get("claimed") is True, claim
    store.upsert_runner_session(
        surrendered_runner(
            claim_id=claim["claim_id"],
            work_session_id=work_session["work_session_id"],
        ), actor="bug270-test", project=P)
    with _conn(P) as c:
        c.execute("UPDATE tasks SET status='Done' WHERE task_id=?", (TASK_ID,))

    pending = store.list_runner_sessions(
        host_id=HOST_ID,
        include_stale=True,
        pending_completion=True,
        project=P,
    )
    assert [row["runner_session_id"] for row in pending] == [RUNNER_ID], pending

    agent_host.subprocess.run = supervisor_list
    agent_host._try = fake_try
    agent_host._persist_pending_stop_receipt = lambda receipt: persisted.append(
        dict(receipt))
    agent_host._delete_pending_stop_receipt = lambda runner_id: deleted.append(
        runner_id)

    drained = agent_host._drain_runners(HOST_ID)
    assert len(drained) == 1, drained
    assert drained[0]["alive"] is False, drained
    assert drained[0]["status"] == "running", drained
    assert (drained[0]["metadata"] or {}).get("lease_surrender"), drained

    outcomes = agent_host.renew_live_direct_runners({"host_id": HOST_ID})
    terminal = [
        body for method, path, body in posted
        if method == "POST" and path == agent_host.P_HEARTBEAT_RUNNER
    ]
    assert len(terminal) == 1, posted
    assert terminal[0]["runner_session_id"] == RUNNER_ID
    assert terminal[0]["status"] == "stopped"
    assert terminal[0]["metadata"]["terminalized_by"] == \
        "terminal_lease_surrendered"
    assert "failure_reason" not in terminal[0]["metadata"]
    assert persisted and persisted[0]["runner_session_id"] == RUNNER_ID
    assert deleted == [RUNNER_ID]
    assert outcomes == [{
        "runner_session_id": RUNNER_ID,
        "task_id": TASK_ID,
        "wake_id": None,
        "terminalized": True,
        "wake_repaired": False,
    }], outcomes

    central = store.list_runner_sessions(
        host_id=HOST_ID, include_stale=True, project=P)
    assert central[0]["status"] == "stopped", central
    assert store.get_work_session(
        work_session["work_session_id"], project=P,
    )["status"] == "completed"
    with _conn(P) as c:
        assert c.execute(
            "SELECT status FROM task_claims WHERE id=?", (claim["claim_id"],),
        ).fetchone()[0] == "completed"
    assert store.list_runner_sessions(
        host_id=HOST_ID,
        include_stale=True,
        pending_completion=True,
        project=P,
    ) == []
finally:
    agent_host.subprocess.run = saved_run
    agent_host._try = saved_try
    agent_host._persist_pending_stop_receipt = saved_persist
    agent_host._delete_pending_stop_receipt = saved_delete
    shutil.rmtree(TMP, ignore_errors=True)


print("BUG-270 terminal surrender receipt tests passed")
