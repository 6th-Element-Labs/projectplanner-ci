#!/usr/bin/env python3
"""WATCH-10: scheduler binding never changes native PTY transport identity."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from switchboard.storage.repositories import runner as runner_repo  # noqa: E402


def native_assignment(*, claim_id="", work_session_id="", phase="preclaim"):
    return {
        "runner_session_id": "run-watch10",
        "task_id": "WATCH-10",
        "claim_id": claim_id,
        "host_id": "host/watch10",
        "status": "running",
        "control": {"runner_open": True},
        "metadata": {
            "wake_id": "wake-watch10",
            "work_session_id": work_session_id,
            "direct_assignment": True,
            "assignment_schema": "switchboard.direct_cli_assignment.v1",
            "native_host_execution": True,
            "credential_admission_phase": phase,
            "pty": True,
            "stream_bind": "127.0.0.1",
            "stream_port": 61110,
        },
    }


unbound = native_assignment()
bound = native_assignment(
    claim_id="taskclaim-watch10",
    work_session_id="worksession-watch10",
    phase="claim_bound",
)

assert runner_repo.is_native_assignment_runner(unbound)
assert runner_repo.is_native_assignment_runner(bound)
assert runner_repo.is_credential_preclaim_runner(unbound)
assert not runner_repo.is_credential_preclaim_runner(bound)

unbound_watch = runner_repo.assert_runner_watchable(unbound)
bound_watch = runner_repo.assert_runner_watchable(bound)
assert unbound_watch["watchable"] is True
assert bound_watch["watchable"] is True
assert unbound_watch["binding_mode"] == "native_assignment"
assert bound_watch["binding_mode"] == "native_assignment"

# Unknown live attachment is also transport truth, independent of assignment or
# scheduler bind shape. It still requires the task/host/wake authorization tuple.
attached = native_assignment()
attached["metadata"].pop("direct_assignment")
attached["metadata"].pop("assignment_schema")
assert not runner_repo.is_native_assignment_runner(attached)
assert runner_repo.assert_runner_watchable(attached, host_attached=True)["watchable"]

print("WATCH-10 transport identity: 11 assertions passed")
