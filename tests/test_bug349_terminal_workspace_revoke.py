#!/usr/bin/env python3
"""BUG-349: terminal acknowledgement releases the implementation workspace."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


HOST = "host/bug349"
RUNNER = "run-bug349"
TASK = "BUG-349"


def exited_completion_runner() -> dict:
    return {
        "runner_session_id": RUNNER,
        "host_id": HOST,
        "task_id": TASK,
        "agent_id": "agent/codex/bug349",
        "runtime": "codex",
        "status": "running",
        "alive": False,
        "_host_project": "switchboard",
        "metadata": {
            "execution_generation": 1,
            "execution_role": "implementation",
            "lease_surrender": {"authority": "completion_owner"},
        },
    }


saved = {
    "runners": agent_host._drain_runner_projects,
    "persist": agent_host._persist_pending_stop_receipt,
    "delete": agent_host._delete_pending_stop_receipt,
    "drop": agent_host._drop_host_bridge,
    "revoke": agent_host.revoke_runner_workspace,
    "try": agent_host._try,
}
events = []
try:
    agent_host._drain_runner_projects = lambda _inventory: [
        exited_completion_runner()
    ]
    agent_host._persist_pending_stop_receipt = lambda _receipt: events.append(
        "persist")
    agent_host._drop_host_bridge = lambda _runner_id: events.append("drop")
    agent_host.revoke_runner_workspace = lambda _runner_id, _reason: (
        events.append("revoke") or {"revoked": True}
    )
    agent_host._try = lambda method, path, body=None: (
        events.append("terminal_ack")
        or {"runner_session_id": RUNNER, "status": "stopped"}
    )
    agent_host._delete_pending_stop_receipt = lambda _runner_id: events.append(
        "delete")

    outcomes = agent_host.renew_live_direct_runners({"host_id": HOST})
finally:
    agent_host._drain_runner_projects = saved["runners"]
    agent_host._persist_pending_stop_receipt = saved["persist"]
    agent_host._delete_pending_stop_receipt = saved["delete"]
    agent_host._drop_host_bridge = saved["drop"]
    agent_host.revoke_runner_workspace = saved["revoke"]
    agent_host._try = saved["try"]

assert events == ["persist", "drop", "revoke", "terminal_ack", "delete"], events
assert outcomes == [{
    "runner_session_id": RUNNER,
    "task_id": TASK,
    "wake_id": None,
    "terminalized": True,
    "wake_repaired": False,
    "workspace_revoked": True,
}], outcomes


# The durable receipt closes the crash window between persistence and teardown.
# Replay must retry the exact workspace revocation, and it must not expose In
# Review while a registered implementation workspace remains writable.
saved_require = agent_host._require
saved_drop = agent_host._drop_host_bridge
saved_revoke = agent_host.revoke_runner_workspace
saved_runner_dir = os.environ.get("PM_RUNNER_DIR")
with tempfile.TemporaryDirectory(prefix="bug349-pending-") as runner_dir:
    os.environ["PM_RUNNER_DIR"] = runner_dir
    agent_host._persist_pending_stop_receipt({
        "project": "switchboard",
        "runner_session_id": RUNNER,
        "host_id": HOST,
        "task_id": TASK,
        "status": "stopped",
        "metadata": {"terminalized_by": "terminal_lease_surrendered"},
    })
    posts = []
    drops = []
    try:
        agent_host._drop_host_bridge = lambda runner_id: drops.append(runner_id)
        agent_host.revoke_runner_workspace = lambda _runner_id, _reason: {
            "revoked": False,
            "error": "workspace_remove_failed",
        }
        agent_host._require = lambda *_args, **_kwargs: posts.append(True) or {
            "status": "stopped"
        }

        blocked = agent_host._drain_pending_stop_receipts(HOST)
        receipt_path = agent_host._pending_stop_receipt_path(RUNNER)
        assert receipt_path.exists(), blocked
        assert posts == [], posts
        assert blocked[0]["expired"] is False, blocked
        assert blocked[0]["workspace_revoked"] is False, blocked
        assert blocked[0]["error"] == "workspace_remove_failed", blocked

        agent_host.revoke_runner_workspace = lambda _runner_id, _reason: {
            "revoked": True,
        }
        replayed = agent_host._drain_pending_stop_receipts(HOST)
        assert replayed[0]["expired"] is True, replayed
        assert replayed[0]["workspace_revoked"] is True, replayed
        assert posts == [True], posts
        assert not receipt_path.exists(), receipt_path
        assert drops == [RUNNER, RUNNER], drops
    finally:
        agent_host._require = saved_require
        agent_host._drop_host_bridge = saved_drop
        agent_host.revoke_runner_workspace = saved_revoke
        if saved_runner_dir is None:
            os.environ.pop("PM_RUNNER_DIR", None)
        else:
            os.environ["PM_RUNNER_DIR"] = saved_runner_dir


print("BUG-349 terminal workspace revoke: PASS")
