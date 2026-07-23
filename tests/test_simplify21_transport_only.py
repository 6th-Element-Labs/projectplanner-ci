#!/usr/bin/env python3
"""SIMPLIFY-21: communication records facts but cannot control execution."""
from __future__ import annotations

import os
import inspect
import tempfile
from pathlib import Path

from path_setup import ROOT

tmp = tempfile.mkdtemp(prefix="simplify21-")
os.environ["PM_DB_PATH"] = str(Path(tmp) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(tmp) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(tmp) / "switchboard.db")

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.contracts.messaging.v1 import SendAgentMessageCommand  # noqa: E402

project = "switchboard"
store.init_db(project)
task = store.create_task(
    {"workstream_id": "SIMPLIFY", "title": "transport invariant", "status": "Ready"},
    actor="test", project=project,
)
agent = "codex/simplify21-recipient"
store.register_agent(agent, "codex", task_id=task["task_id"], ttl_s=300,
                     project=project)
claim = store.claim_task(task["task_id"], agent_id=agent, project=project)
assert claim.get("claim_id"), claim
work_session = store.create_work_session({
    "schema": "switchboard.work_session.v1",
    "project_id": project,
    "agent_id": agent,
    "task_id": task["task_id"],
    "claim_id": claim["claim_id"],
    "repo_role": "canonical",
    "storage_mode": "external",
    "dirty_status": "clean",
    "status": "active",
}, actor=agent, project=project)["work_session"]

with _conn(project) as connection:
    before = {
        "claim": [dict(row) for row in connection.execute(
            "SELECT * FROM task_claims WHERE id=?", (claim["claim_id"],))],
        "work_session": [dict(row) for row in connection.execute(
            "SELECT * FROM work_sessions WHERE work_session_id=?",
            (work_session["work_session_id"],))],
        "wakes": [dict(row) for row in connection.execute(
            "SELECT * FROM wake_intents ORDER BY wake_id")],
        "runners": [dict(row) for row in connection.execute(
            "SELECT * FROM runner_sessions ORDER BY runner_session_id")],
        "leases": [dict(row) for row in connection.execute(
            "SELECT * FROM resource_leases ORDER BY id")],
    }

message = store.send_agent_message(
    "codex/simplify21-sender", agent, "two-second delivery expectation",
    task_id=task["task_id"], requires_ack=True, ack_timeout_seconds=2,
    project=project,
)
expectation = message.get("ack_expectation") or {}
assert expectation.get("status") == "below_delivery_floor", expectation
assert expectation.get("execution_effect") == "none", expectation
assert expectation.get("floor_seconds", 0) >= 300.0, expectation
store.sweep_coordination_monitors(project=project, now=message["sent_at"] + 3)
# Duplicate timeout handling and restart are idempotent: a second sweep at a
# later clock re-fires nothing and creates no second notice.
resweep = store.sweep_coordination_monitors(project=project,
                                            now=message["sent_at"] + 300)
assert resweep["fired"] == 0, resweep

with _conn(project) as connection:
    after = {
        "claim": [dict(row) for row in connection.execute(
            "SELECT * FROM task_claims WHERE id=?", (claim["claim_id"],))],
        "work_session": [dict(row) for row in connection.execute(
            "SELECT * FROM work_sessions WHERE work_session_id=?",
            (work_session["work_session_id"],))],
        "wakes": [dict(row) for row in connection.execute(
            "SELECT * FROM wake_intents ORDER BY wake_id")],
        "runners": [dict(row) for row in connection.execute(
            "SELECT * FROM runner_sessions ORDER BY runner_session_id")],
        "leases": [dict(row) for row in connection.execute(
            "SELECT * FROM resource_leases ORDER BY id")],
    }

assert after == before, "ack timeout mutated lifecycle or execution state"
status = store.get_message_status(message["id"], project=project)
assert status["monitor"]["status"] == "fired"
notices = store.list_agent_messages(project=project, agent="codex/simplify21-sender")
assert any(row["signal"] == "ack_timeout" and not row["requires_ack"] for row in notices)

agents = store.list_active_agents(project=project)
recipient = next(row for row in agents if row["agent_id"] == agent)
assert recipient["mailbox"]["unacked_count"] == 1
assert recipient["mailbox"]["oldest_unacked_age_seconds"] >= 0
assert recipient["mailbox"]["stale_is_lifecycle_authority"] is False

try:
    SendAgentMessageCommand.from_mapping({
        "from_agent": "a", "to_agent": "b", "message": "legacy",
        "on_ack_timeout": "wake" + "_target",
    })
except ValueError:
    pass
else:
    raise AssertionError("legacy lifecycle timeout action was accepted")

from switchboard.storage.repositories import coordination  # noqa: E402

text = "\n".join((
    inspect.getsource(coordination.send_agent_message),
    inspect.getsource(coordination._create_ack_monitor),
    inspect.getsource(coordination.sweep_coordination_monitors),
))
for forbidden in (
    "start_task(", "request_wake(", "request_runner_control(",
    "enqueue_merge(", "revoke_claim(",
):
    assert forbidden not in text, f"communication repository imports lifecycle action: {forbidden}"

print("PASS SIMPLIFY-21 transport invariants")
