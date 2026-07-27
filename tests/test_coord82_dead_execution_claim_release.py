#!/usr/bin/env python3
"""COORD-82: a dead bound execution releases ownership before dispatch."""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
import time

from path_setup import ROOT  # noqa: F401


TMP = tempfile.mkdtemp(prefix="coord82-dead-claim-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP

if sys.version_info < (3, 10):
    _dataclass = dataclasses.dataclass

    def _compat_dataclass(*args, **kwargs):
        kwargs.pop("slots", None)
        return _dataclass(*args, **kwargs)

    dataclasses.dataclass = _compat_dataclass

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from execution_readiness_fixture import configure_ready_project  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402


P = "switchboard"
OLD_AGENT = "agent/codex/dead-owner"
NEW_AGENT = "agent/codex/replacement"


def seed_dead_bound_execution(task_id: str, ordinal: int) -> str:
    claim = store.claim_task(task_id, OLD_AGENT, actor="coord82-test", project=P)
    claim_id = claim["claim_id"]
    runner_id = f"run_coord82_dead_{ordinal}"
    stale_at = time.time() - 100_000
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": "host/coord82",
        "agent_id": OLD_AGENT,
        "runtime": "codex",
        "task_id": task_id,
        # Production race: Connect spawned before claim, then died before the
        # next heartbeat copied claim_id onto runner_sessions.
        "claim_id": "",
        "status": "running",
        "heartbeat_at": stale_at,
        "heartbeat_ttl_s": 180,
        "metadata": {"connect_assignment": True},
    }, actor="coord82-test", project=P)
    with _conn(P) as c:
        c.execute(
            "UPDATE task_claims SET runner_session_id=?,execution_generation=1,"
            "execution_role='implementation',lease_epoch=1 WHERE id=?",
            (runner_id, claim_id),
        )

    # The capacity reaper reports the ordinary surrendered/host-loss shape:
    # stored status was still "running", but the full lease predicate is dead.
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": "host/coord82",
        "agent_id": OLD_AGENT,
        "runtime": "codex",
        "task_id": task_id,
        "status": "expired",
        "metadata": {
            "terminalized_by": "runner_lease_expiry",
            "failure_reason": "runner heartbeat lease expired",
        },
    }, actor="host/coord82", project=P)
    return claim_id


try:
    store.init_db(P)
    configure_ready_project(P, actor="coord82-test")
    task_ids = [
        store.create_task(
            {"workstream_id": "COORD", "title": f"COORD-82 dispatch gate {i}"},
            actor="coord82-test", project=P,
        )["task_id"]
        for i in range(4)
    ]
    claim_ids = [
        seed_dead_bound_execution(task_id, i)
        for i, task_id in enumerate(task_ids)
    ]
    store.register_agent(
        NEW_AGENT, "codex", lane="COORD", ttl_s=300,
        actor="coord82-test", project=P,
    )

    # Ownership with no execution binding, a missing runner row, or a genuinely
    # live runner remains exclusive. None is safe evidence that execution died.
    control_tasks = [
        store.create_task(
            {"workstream_id": "COORD", "title": f"COORD-82 blocking control {i}"},
            actor="coord82-test", project=P,
        )["task_id"]
        for i in range(3)
    ]
    control_claims = [
        store.claim_task(task_id, OLD_AGENT, actor="coord82-test", project=P)
        for task_id in control_tasks
    ]
    with _conn(P) as c:
        c.execute(
            "UPDATE task_claims SET runner_session_id='run_missing' WHERE id=?",
            (control_claims[1]["claim_id"],),
        )
    store.upsert_runner_session({
        "runner_session_id": "run_coord82_live",
        "host_id": "host/coord82",
        "agent_id": OLD_AGENT,
        "runtime": "codex",
        "task_id": control_tasks[2],
        "claim_id": control_claims[2]["claim_id"],
        "status": "running",
        "heartbeat_at": time.time(),
        "heartbeat_ttl_s": 180,
    }, actor="coord82-test", project=P)
    for task_id in control_tasks:
        blocked = store.request_wake(
            {"agent_id": NEW_AGENT, "runtime": "codex"},
            task_id=task_id, caller_agent_id=NEW_AGENT,
            enforce_task_ownership=True, reason="COORD-82 control",
            actor="coord82-test", project=P,
        )
        assert blocked.get("error") == "active_claim_conflict", blocked

    with _conn(P) as c:
        rows = c.execute(
            "SELECT id,status FROM task_claims WHERE id IN (?,?,?,?)",
            claim_ids,
        ).fetchall()
        assert {row["status"] for row in rows} == {"abandoned"}, rows
        events = c.execute(
            "SELECT payload FROM activity "
            "WHERE kind='task.claim.released_by_terminal_runner'",
        ).fetchall()
        assert len(events) == 4, events

    # All dispatch readers now agree because the ownership row was mutated
    # once, audibly, at execution death rather than filtered at read time.
    next_claim = store.claim_next(
        NEW_AGENT, lanes=["COORD"], actor="coord82-test", project=P,
    )
    assert next_claim.get("claimed") is True, next_claim
    claimed_by_next = next_claim["task"]["task_id"]
    store.abandon_claim(
        next_claim["claim_id"], "continue gate coverage",
        actor="coord82-test", project=P,
    )

    exact_task = next(t for t in task_ids if t != claimed_by_next)
    exact_claim = store.claim_task(
        exact_task, NEW_AGENT, actor="coord82-test", project=P,
    )
    assert exact_claim.get("claimed") is True, exact_claim
    store.abandon_claim(
        exact_claim["claim_id"], "continue gate coverage",
        actor="coord82-test", project=P,
    )

    wake_task = next(t for t in task_ids if t not in {claimed_by_next, exact_task})
    wake = store.request_wake(
        {"agent_id": NEW_AGENT, "runtime": "codex"},
        task_id=wake_task, caller_agent_id=NEW_AGENT,
        enforce_task_ownership=True, reason="COORD-82 gate proof",
        actor="coord82-test", project=P,
    )
    assert wake.get("wake_id") and wake.get("status") == "pending", wake

    start_task_id = next(
        t for t in task_ids if t not in {claimed_by_next, exact_task, wake_task}
    )
    started = task_execution.start_task(
        start_task_id, agent_id=NEW_AGENT,
        actor="coord82-test", project=P,
    )
    assert started.get("started") is True, started
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("COORD-82 dead execution claim release: passed")
