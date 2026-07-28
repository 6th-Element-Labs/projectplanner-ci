#!/usr/bin/env python3
"""A forgotten local runner still receives its exact Host terminal ack."""
from __future__ import annotations

import json
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


HOST_ID = "host/bug218"
RUNNER_ID = "run-bug218"
TASK_ID = "BUG-218"


def pending_runner():
    return {
        "runner_session_id": RUNNER_ID,
        "host_id": HOST_ID,
        "task_id": TASK_ID,
        "claim_id": "claim-bug218",
        "agent_id": "agent/codex/bug-218",
        "runtime": "codex",
        "status": "running",
        "metadata": {
            "execution_generation": 3,
            "execution_role": "remediation",
            "lease_epoch": 2,
            "completion_handoff": {
                "execution_id": RUNNER_ID,
                "claim_id": "claim-bug218",
                "task_id": TASK_ID,
                "generation": 3,
                "lease_epoch": 2,
                "host_id": HOST_ID,
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
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"sessions": []}),
        stderr="",
    )


def fake_try(method, path, body=None):
    if method == "GET":
        assert "pending_completion=true" in path, path
        return {"sessions": [pending_runner()]}
    posted.append((method, path, dict(body or {})))
    return {"runner_session_id": RUNNER_ID, "status": "exited"}


try:
    agent_host.subprocess.run = supervisor_list
    agent_host._try = fake_try
    agent_host._persist_pending_stop_receipt = lambda receipt: persisted.append(
        dict(receipt))
    agent_host._delete_pending_stop_receipt = lambda runner_id: deleted.append(
        runner_id)

    drained = agent_host._drain_runners(HOST_ID)
    assert len(drained) == 1, drained
    assert drained[0]["runner_session_id"] == RUNNER_ID
    assert drained[0]["alive"] is False

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

    # If the supervisor cannot be read, absence is not process-death proof.
    posted.clear()
    agent_host.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="unavailable")
    unavailable = agent_host._drain_runners(HOST_ID)
    assert unavailable and unavailable[0].get("alive") is not False
    agent_host.renew_live_direct_runners({"host_id": HOST_ID})
    assert not posted
finally:
    agent_host.subprocess.run = saved_run
    agent_host._try = saved_try
    agent_host._persist_pending_stop_receipt = saved_persist
    agent_host._delete_pending_stop_receipt = saved_delete

print("BUG-218 forgotten completion handoff tests passed")
