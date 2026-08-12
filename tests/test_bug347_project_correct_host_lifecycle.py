#!/usr/bin/env python3
"""Agent Host keeps lifecycle receipts on the task's project."""
from __future__ import annotations

import json
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


PROJECT = "simplemark"
RUNNER_ID = "run-bug347"
TASK_ID = "BUG-347"
HOST_ID = "host/bug347"


def session(*, alive: bool) -> dict:
    return {
        "runner_session_id": RUNNER_ID,
        "agent_id": "agent/codex/bug-347",
        "runtime": "codex",
        "task_id": TASK_ID,
        "host_id": HOST_ID,
        "status": "running",
        "alive": alive,
        "_host_project": PROJECT,
        "metadata": {
            "wake_id": "wake-bug347",
            "native_host_execution": True,
            "connect_assignment": True,
        },
    }


saved_drain = agent_host._drain_runners
saved_drain_projects = agent_host._drain_runner_projects
saved_try = agent_host._try
saved_run = agent_host.subprocess.run
saved_persist = agent_host._persist_pending_stop_receipt
saved_delete = agent_host._delete_pending_stop_receipt
posted = []
persisted = []
deleted = []


def fake_try(method, path, body=None):
    posted.append((method, path, dict(body or {})))
    return {"runner_session_id": RUNNER_ID, "status": (body or {}).get("status")}


try:
    agent_host.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"sessions": [session(alive=False)]}),
        stderr="",
    )
    agent_host._try = lambda *_args, **_kwargs: {
        "sessions": [session(alive=True)]}
    joined = agent_host._drain_runners(
        HOST_ID, recover_stale_local=False, project=PROJECT)
    assert joined and joined[0]["alive"] is False, joined

    agent_host._try = fake_try
    agent_host._persist_pending_stop_receipt = lambda receipt: persisted.append(
        dict(receipt))
    agent_host._delete_pending_stop_receipt = lambda runner_id: deleted.append(runner_id)

    agent_host._drain_runner_projects = lambda _inventory: [session(alive=True)]
    agent_host.renew_live_direct_runners({"host_id": HOST_ID, "repo_root": str(ROOT)})
    running = [body for method, path, body in posted
               if method == "POST" and path == agent_host.P_HEARTBEAT_RUNNER]
    assert running and running[-1]["project"] == PROJECT, running

    posted.clear()
    agent_host._drain_runner_projects = lambda _inventory: [session(alive=False)]
    agent_host.renew_live_direct_runners({"host_id": HOST_ID, "repo_root": str(ROOT)})
    terminal = [body for method, path, body in posted
                if method == "POST" and path == agent_host.P_HEARTBEAT_RUNNER]
    assert terminal and terminal[-1]["project"] == PROJECT, terminal
    assert persisted and persisted[-1]["project"] == PROJECT, persisted
    assert deleted == [RUNNER_ID], deleted
finally:
    agent_host._drain_runners = saved_drain
    agent_host._drain_runner_projects = saved_drain_projects
    agent_host._try = saved_try
    agent_host.subprocess.run = saved_run
    agent_host._persist_pending_stop_receipt = saved_persist
    agent_host._delete_pending_stop_receipt = saved_delete

print("BUG-347 project-correct Host lifecycle: PASS")
