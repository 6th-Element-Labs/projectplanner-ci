#!/usr/bin/env python3
"""BUG-337: Agent Host retains retryable completion acknowledgements."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


HOST = "host/bug337-retry"
RUNNER = "run-bug337-retry"
TASK = "BUG-337"
TMP = Path(tempfile.mkdtemp(prefix="bug337-agent-host-receipt-"))
os.environ["PM_RUNNER_DIR"] = str(TMP)


def pending_runner() -> dict:
    return {
        "runner_session_id": RUNNER,
        "host_id": HOST,
        "task_id": TASK,
        "claim_id": "claim-bug337-retry",
        "agent_id": "agent/codex/bug-337",
        "runtime": "codex",
        "status": "running",
        "metadata": {
            "execution_generation": 1,
            "execution_role": "implementation",
            "lease_epoch": 2,
            "completion_handoff": {
                "execution_id": RUNNER,
                "claim_id": "claim-bug337-retry",
                "task_id": TASK,
                "generation": 1,
                "lease_epoch": 2,
                "host_id": HOST,
            },
        },
    }


pending_response = {
    "runner_session_id": RUNNER,
    "status": "exited",
    "completion_handoff_pending": True,
    "error_code": "terminal_ack_execution_lease_invalid",
}
saved_run = agent_host.subprocess.run
saved_try = agent_host._try
saved_require = agent_host._require


try:
    agent_host.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"sessions": []}), stderr="")

    def fake_try(method, path, body=None):
        if method == "GET":
            return {"sessions": [pending_runner()]}
        return dict(pending_response)

    agent_host._try = fake_try
    outcomes = agent_host.renew_live_direct_runners({"host_id": HOST})
    receipt_path = agent_host._pending_stop_receipt_path(RUNNER)
    assert receipt_path.exists(), outcomes
    assert outcomes[0]["terminalized"] is False, outcomes

    agent_host._require = lambda *_args, **_kwargs: dict(pending_response)
    retried = agent_host._drain_pending_stop_receipts(HOST)
    assert retried[0]["expired"] is False, retried
    assert receipt_path.exists(), retried
finally:
    agent_host.subprocess.run = saved_run
    agent_host._try = saved_try
    agent_host._require = saved_require

print("BUG-337 Agent Host pending receipt retry: PASS")
