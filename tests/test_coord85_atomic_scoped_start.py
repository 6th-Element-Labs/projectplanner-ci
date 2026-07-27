#!/usr/bin/env python3
"""COORD-85: scoped Start and capacity admission share one generation lifetime."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from path_setup import ROOT, SRC  # noqa: F401

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="coord85-")
DB = str(Path(TMP) / "switchboard.db")
os.environ["PM_DB_PATH"] = DB
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = DB
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

from db.connection import _conn  # noqa: E402
from db.schema import apply_schema  # noqa: E402
from switchboard.storage.repositories import coordination  # noqa: E402

PROJECT = "switchboard"
TASK = "COORD-85"


with sqlite3.connect(DB) as raw:
    raw.row_factory = sqlite3.Row
    apply_schema(raw)


def start(idem_key: str) -> dict:
    return coordination.request_wake(
        selector={"runtime": "codex", "agent_id": "agent/codex/coord-85"},
        reason=f"Connect assignment {TASK}",
        source="connect",
        task_id=TASK,
        actor="operator-test",
        project=PROJECT,
        idem_key=idem_key,
        policy={
            "deadline_seconds": 30,
            "no_eligible_host": "fail",
            "assignment": {
                "schema": "switchboard.connect.assignment.v1",
                "assignment_id": f"assignment-{idem_key}",
            },
            "lifecycle": {
                "schema": "switchboard.execution_lifecycle.v1",
                "role": "implementation",
                "head_sha": "",
                "ttl_seconds": 7200,
            },
            "coordination_scope": {
                "schema": "switchboard.scoped_start_request.v1",
                "scope_type": "task",
                "task_project": PROJECT,
                "task_id": TASK,
                "runtime": "codex",
                "started_by": "operator-test",
            },
        },
    )


first = start("coord85:first")
assert first["status"] == "failed", first
assert first["scope"]["scope_id"], first

with _conn(PROJECT) as conn:
    wake = conn.execute(
        "SELECT deadline,requested_at FROM wake_intents WHERE wake_id=?",
        (first["wake_id"],),
    ).fetchone()
    lease = conn.execute(
        "SELECT * FROM resource_leases WHERE wake_id=?", (first["wake_id"],),
    ).fetchone()
    scope = conn.execute(
        "SELECT * FROM autopilot_scopes WHERE task_id=?", (TASK,),
    ).fetchone()
    assert wake and lease and scope
    assert int(wake["deadline"] - wake["requested_at"]) == int(lease["ttl_seconds"]) == 30
    assert lease["lease_state"] == "expired" and lease["released_at"] is not None
    assert int(lease["fence_epoch"]) == 2

second = start("coord85:second")
assert second["status"] == "failed", second
assert second["wake_id"] != first["wake_id"]
assert second["scope"]["scope_id"] == first["scope"]["scope_id"]

with _conn(PROJECT) as conn:
    leases = conn.execute(
        "SELECT execution_generation,wake_id,lease_state,fence_epoch "
        "FROM resource_leases WHERE task_id=? ORDER BY execution_generation",
        (TASK,),
    ).fetchall()
    assert [row["execution_generation"] for row in leases] == [1, 2]
    assert all(row["lease_state"] == "expired" for row in leases)
    assert all(int(row["fence_epoch"]) == 2 for row in leases)
    scope = conn.execute(
        "SELECT last_result_json,started_by,started_at FROM autopilot_scopes "
        "WHERE task_id=?", (TASK,),
    ).fetchone()
    assert scope["started_by"] == "operator-test" and scope["started_at"]
    assert second["wake_id"] in scope["last_result_json"]

# External-effect refusal happens after execution identity and scope creation but
# before the wake row exists. That mid-transaction refusal must surrender the
# reserved lease so a later Start receives a fresh generation.
facade = coordination._store_facade()
saved_claim_effect = facade._claim_external_effect_in
facade._claim_external_effect_in = lambda *_args, **_kwargs: {
    "claimed": False,
    "reason": "in_progress",
    "effect": {},
    "effect_key": "effect-refused",
}
try:
    refused = start("coord85:effect-refused")
finally:
    facade._claim_external_effect_in = saved_claim_effect
assert refused["requested"] is False, refused
with _conn(PROJECT) as conn:
    refused_lease = conn.execute(
        "SELECT * FROM resource_leases WHERE task_id=? "
        "ORDER BY execution_generation DESC LIMIT 1",
        (TASK,),
    ).fetchone()
    assert refused_lease["released_at"] is not None
    assert refused_lease["lease_state"] == "expired"
    assert int(refused_lease["fence_epoch"]) == 2
    refused_generation = int(refused_lease["execution_generation"])

after_refusal = start("coord85:after-effect-refused")
with _conn(PROJECT) as conn:
    fresh_lease = conn.execute(
        "SELECT * FROM resource_leases WHERE wake_id=?",
        (after_refusal["wake_id"],),
    ).fetchone()
    assert int(fresh_lease["execution_generation"]) == refused_generation + 1


def revive_terminal_wake(
        idem_key: str, *, status: str, now: float,
        deadline: float, claimed_at: float | None = None,
        policy_updates: dict | None = None,
        placement: dict | None = None) -> dict:
    """Mint a real lease/wake pair, then stage one terminal-path precondition."""
    wake = start(idem_key)
    policy = dict(wake.get("policy") or {})
    policy.update(policy_updates or {})
    with _conn(PROJECT) as conn:
        conn.execute(
            "UPDATE wake_intents SET status=?,completed_at=NULL,deadline=?,"
            "claimed_at=?,runner_session_id=NULL,policy_json=?,placement_json=? "
            "WHERE wake_id=?",
            (status, deadline, claimed_at,
             json.dumps(policy, sort_keys=True),
             json.dumps(placement or {}, sort_keys=True),
             wake["wake_id"]),
        )
        conn.execute(
            "UPDATE resource_leases SET released_at=NULL,lease_state='reserved',"
            "fence_epoch=1 WHERE wake_id=?",
            (wake["wake_id"],),
        )
    return wake


def assert_surrendered(wake_id: str, label: str) -> None:
    with _conn(PROJECT) as conn:
        lease = conn.execute(
            "SELECT released_at,lease_state,fence_epoch FROM resource_leases "
            "WHERE wake_id=?", (wake_id,),
        ).fetchone()
    assert lease["released_at"] is not None, label
    assert lease["lease_state"] == "expired", label
    assert int(lease["fence_epoch"]) == 2, label


# Terminal-path setup differs, but the resulting invariant is asserted from one
# table so every new path must prove exact lease release and fence advancement.
clock = time.time()
terminal_cases: list[tuple[str, str]] = []
cancelled = revive_terminal_wake(
    "coord85:cancel", status="pending", now=clock, deadline=clock + 60)
coordination.cancel_wake(
    cancelled["wake_id"], actor="coord85-test", project=PROJECT)
terminal_cases.append((cancelled["wake_id"], "cancel_wake"))

host_id = "host/coord85-deadline"
facade.register_host({
    "host_id": host_id,
    "runtimes": [{"runtime": "codex", "lanes": ["COORD"]}],
    "limits": {"max_sessions": 1},
    "heartbeat_ttl_s": 60,
}, actor="coord85-test", project=PROJECT)
claim_deadline = revive_terminal_wake(
    "coord85:claim-deadline", status="pending", now=clock,
    deadline=clock - 1)
claim_result = coordination.claim_wake(
    host_id, claim_deadline["wake_id"],
    actor="coord85-test", project=PROJECT)
assert claim_result["reason"] == "deadline_expired", claim_result
terminal_cases.append(
    (claim_deadline["wake_id"], "claim-time deadline expiry"))

claim_hold = revive_terminal_wake(
    "coord85:claim-hold", status="claimed", now=clock,
    deadline=clock + 60, claimed_at=clock - 600,
    policy_updates={"mode": "connect"})
coordination.sweep_wake_intents(project=PROJECT, now=clock)
terminal_cases.append(
    (claim_hold["wake_id"], "connect claim-hold expiry"))

host_loss = revive_terminal_wake(
    "coord85:host-loss", status="pending", now=clock,
    deadline=clock + 60,
    policy_updates={"scheduler": {"max_host_loss_reschedules": 3}},
    placement={
        "scheduler_mode": "hybrid",
        "selected_host_id": "host/missing",
        "host_loss_recovery_count": 3,
    })
host_loss_sweep = coordination.sweep_wake_intents(project=PROJECT, now=clock)
assert any(
    event.get("wake_id") == host_loss["wake_id"]
    and event.get("reason") == "host_loss_recovery_exhausted"
    for event in host_loss_sweep["events"]
), host_loss_sweep
terminal_cases.append(
    (host_loss["wake_id"], "host-loss recovery exhaustion"))

for terminal_wake_id, terminal_label in terminal_cases:
    assert_surrendered(terminal_wake_id, terminal_label)

print("coord85 atomic scoped Start: passed")
