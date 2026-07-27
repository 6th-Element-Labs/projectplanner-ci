"""Shared runtime for the gold decision-quality catalog.

Both the builder (``scripts/completion_conformance/build_gold_catalog.py``)
and the gate (``tests/conformance/gold_decisions.py``) must drive one
world through the *same* real two-tick production path, so a scenario that
built gold is guaranteed re-checkable by the gate with identical semantics.
This module is that one path, so neither caller can drift from the other.

It extends T1's ``tests/conformance/_shared.hermetic_completion_patches``
with the one seam that patch set does not cover: ``route=human``'s
``escalate_human`` effect writes ``completion_runs`` and
``attention_requests`` through their own ``_conn`` / ``_write_through``
seams (``executor._escalate_human``), not through the mutating-effect
ledger (``external_effects.claim_external_effect`` /
``verify_external_effect``) that ``_shared`` already patches. The minimal
schema below is exactly what ``tests/test_coord46_human_attention.py``
proved sufficient for that path.

It also fixes an assumption T1's own ``run_scenario`` gets away with only
because none of its three scenarios exercise it: not every effect invokes a
``CompletionEffectAdapters`` callback. ``wait``, ``none``, and
``attach_and_wait`` are terminal inside ``execute_effect`` and never call an
adapter; asserting "the adapter fired exactly once" for those effects is
simply the wrong invariant, not a failure of the scenario.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
ROOT = TESTS.parent
SRC = ROOT / "src"
for _entry in (str(HERE), str(TESTS), str(SRC), str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from switchboard.application import completion_driver  # noqa: E402
from switchboard.domain.completion.executor import (  # noqa: E402
    CompletionEffectAdapters,
)
from switchboard.storage.migrations import runner as migrations  # noqa: E402
from switchboard.storage.migrations.attention import (  # noqa: E402
    upgrade_attention_schema,
)
from switchboard.storage.repositories import attention as attention_repo  # noqa: E402
from switchboard.storage.repositories import completion_runs  # noqa: E402

import _shared  # noqa: E402


#: The exact migration subset test_coord46_human_attention.py proved
#: sufficient to drive completion_runs + attention through a real escalation.
_HUMAN_SCHEMA_MIGRATIONS = {
    "0074_task_execution_completion_phases",
    "0075_ix_task_execution_completion_identity",
    "0111_completion_runs",
    "0112_ux_completion_runs_task",
}

#: Effects driven through the mutating-effect ledger
#: (``executor._execute_mutating_effect``) -- the only ones that actually
#: invoke a ``CompletionEffectAdapters`` callback.
_LEDGER_EFFECTS = frozenset({
    "ensure_review_generation", "start_remediation", "mark_ready", "enqueue",
    "update_branch", "repair_dispatch", "fence_runner",
    "reconcile_provenance",
})

#: Effects whose receipt carries no ``idempotent_replay`` key at all (they
#: are terminal inside ``execute_effect`` and never reach a ledger or the
#: human-escalation write path).
_NO_REPLAY_RECEIPT_EFFECTS = frozenset({"wait", "none", "attach_and_wait"})


def _build_human_route_db(task_id: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE tasks ("
        "task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
        "assignee TEXT, updated_at REAL)")
    db.execute(
        "CREATE TABLE task_git_state ("
        "task_id TEXT PRIMARY KEY, pr_number INTEGER, head_sha TEXT, "
        "branch TEXT, pr_url TEXT, merged_sha TEXT, evidence_json TEXT)")
    db.execute(
        "CREATE TABLE autopilot_scopes ("
        "scope_id TEXT PRIMARY KEY, status TEXT, lease_id TEXT, "
        "holder_agent_id TEXT, generation INTEGER, fence_epoch INTEGER, "
        "expires_at REAL, scope_type TEXT, task_project TEXT, task_id TEXT, "
        "deliverable_id TEXT)")
    for name, sql in migrations.DDL_MIGRATIONS:
        if name in _HUMAN_SCHEMA_MIGRATIONS:
            db.execute(sql)
    upgrade_attention_schema(db)
    db.execute(
        "INSERT INTO tasks(task_id, status, assignee, updated_at) "
        "VALUES (?,?,?,?)",
        (task_id, "In Review", None, 1.0),
    )
    db.execute(
        "INSERT INTO task_git_state("
        "task_id, pr_number, head_sha, branch, pr_url, merged_sha, evidence_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, _shared.PR_NUMBER, _shared.HEAD, "conformance/gold",
         _shared.PR_URL, None, "{}"),
    )
    db.commit()
    return db


@contextmanager
def _human_route_db_patches(task_id: str) -> Iterator[None]:
    db = _build_human_route_db(task_id)
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(completion_runs, "_conn", return_value=db))
            stack.enter_context(patch.object(
                completion_runs, "_write_through",
                side_effect=lambda _project, fn: fn()))
            stack.enter_context(
                patch.object(attention_repo, "_conn", return_value=db))
            stack.enter_context(patch.object(
                attention_repo, "_write_through",
                side_effect=lambda _project, fn: fn()))
            yield
    finally:
        db.close()


def run_world(
    scenario_id: str, world: dict[str, Any], *, task_id: str = "COORD-65",
) -> dict[str, Any]:
    """Drive the real two-tick production tick for one world, hermetically.

    Same double-tick idempotency proof as
    ``test_completion_conformance.run_scenario``, generalized to also cover
    ``route=human`` and to effects that never call an adapter at all.
    """
    calls: list[dict[str, Any]] = []
    ledger = _shared.EffectLedger()
    run = {
        "run_id": f"gold-run-{scenario_id}",
        "state_version": 1,
        "attempt": 0,
    }

    def effect(plan: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(plan))
        return {"action": "completed", "scenario_id": scenario_id}

    adapters = CompletionEffectAdapters(
        ensure_review_generation=effect,
        start_remediation=effect,
        mark_ready=effect,
        update_branch=effect,
        enqueue=effect,
        repair_dispatch=effect,
        fence_runner=effect,
        reconcile_provenance=effect,
    )
    scenario_stub = {"id": scenario_id, "world": world}
    snapshot = _shared.build_snapshot(scenario_stub, task_id=task_id)

    def _tick() -> dict[str, Any]:
        return completion_driver.run_completion_tick(
            task_id,
            project="switchboard",
            actor="conformance",
            agent_id="conformance",
            store_mod=object(),
            hydrator=lambda *_a, **_k: snapshot,
            adapters=adapters,
        )

    with _shared.hermetic_completion_patches(run, ledger):
        with _human_route_db_patches(task_id):
            first = _tick()
            second = _tick()

    effect_name = str(first["plan"].get("effect") or "")
    expected_calls = 1 if effect_name in _LEDGER_EFFECTS else 0
    _shared.require(
        first["decision"] == second["decision"],
        f"{scenario_id}: second tick changed the decision",
    )
    _shared.require(
        first["plan"]["idem_key"] == second["plan"]["idem_key"],
        f"{scenario_id}: second tick changed effect identity",
    )
    _shared.require(
        len(calls) == expected_calls,
        f"{scenario_id}: effect adapter fired {len(calls)} times, expected "
        f"{expected_calls} for effect {effect_name!r}",
    )
    if effect_name not in _NO_REPLAY_RECEIPT_EFFECTS:
        _shared.require(
            second["execution"]["receipt"].get("idempotent_replay") is True,
            f"{scenario_id}: second tick was not a verified replay",
        )
    return first


def decision_expect(result: dict[str, Any]) -> dict[str, Any]:
    """Project one tick result onto the ``expect`` vocabulary."""
    decision = result["decision"]
    plan = result["plan"]
    roles = [decision["desired_role"]] if decision.get("desired_role") else []
    expect = {
        "terminal": _shared.terminal_for(result),
        "reason_code": decision.get("reason_code"),
        "role_sequence": roles,
        "route": decision.get("route"),
        "effect": plan.get("effect"),
    }
    # COORD-78: only when the refusal actually names a near miss. Writing an
    # empty list into every row would put a field in 24 gold files that asserts
    # nothing, and would make "the near-miss identity survived" indistinguishable
    # from "there was never a near miss to carry".
    near_miss = _shared.missing_artifact_near_miss_keys(decision)
    if near_miss:
        expect["missing_artifact_near_miss_keys"] = near_miss
    return expect


def assert_gold_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Re-check one already-promoted gold scenario against the real tick."""
    first = run_world(scenario["id"], scenario["world"])
    expect = scenario["expect"]
    actual = decision_expect(first)
    for key, expected_value in expect.items():
        _shared.require(
            actual.get(key) == expected_value,
            f"{scenario['id']}: {key} {actual.get(key)!r} != {expected_value!r}",
        )
    return {"scenario_id": scenario["id"], **actual, "second_tick": "idempotent_replay"}


__all__ = ["run_world", "decision_expect", "assert_gold_scenario"]
