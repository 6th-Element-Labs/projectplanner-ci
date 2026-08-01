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


P = "switchboard"
HOST_ID = "host/bug270"
RUNNER_ID = "run_bug270_review"
TASK_ID = "BUG-270"


def surrendered_runner():
    return {
        "runner_session_id": RUNNER_ID,
        "host_id": HOST_ID,
        "agent_id": "agent/codex/bug270",
        "runtime": "codex",
        "task_id": TASK_ID,
        "status": "running",
        "heartbeat_ttl_s": 180,
        "metadata": {
            "native_host_execution": True,
            "connect_assignment": True,
            "execution_id": "exec-bug270",
            "execution_generation": 1,
            "execution_role": "review_merge",
            "lease_epoch": 2,
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
    store.upsert_runner_session(
        surrendered_runner(), actor="bug270-test", project=P)

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
    assert terminal[0]["status"] == "exited"
    assert terminal[0]["metadata"]["terminalized_by"] == "host_supervisor"
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
    assert central[0]["status"] == "exited", central
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
