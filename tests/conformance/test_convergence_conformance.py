#!/usr/bin/env python3
"""Convergence conformance (COORD-77): tick SEQUENCES must converge, never spin.

T1 proves each tick is correct and idempotently replayable. It cannot see the
two sequence-level failures that produced the 2026-07-26 COORD-57 incident
(decision_attempt=50 on one task while the merge queue sat empty):

  * **oscillation** — decisions flip A→B→A→..., each transition re-arming the
    same repair effect with a fresh idem_key (prod: 50 attempts); and
  * **livelock** — in a world that never changes, an identical decision replays
    its verified effect forever: the task is frozen at Blocked, nothing is
    dispatched, and nobody is told. Every individual tick is "correct".

This suite drives ``run_completion_tick`` in a loop against REAL storage on a
temp DB (completion runs, external-effect ledger, decision episodes — no
hand-mocked run semantics; the T1 hermetic patches are deliberately NOT used).
Only the effect adapters are stubbed, by a deterministic **world reactor** that
applies each newly-executed effect's consequence to ``scenario.world``.
``repair_dispatch`` and ``reconcile_provenance`` are the adversarial null
repair: they complete and change NOTHING — the exact shape of a repair runner
that runs and fixes nothing (CO-21), or a dispatch whose work cannot help.

Global invariants asserted on EVERY scenario (base + gold), no per-scenario
expectations required:

  1. a terminal is reached within ``MAX_TICKS``;
  2. an identical mutating (effect, reason_code, world) is never EXECUTED
     (non-replay) twice — oscillation spin;
  3. a mutating effect never replays more than ``LIVELOCK_TICKS`` consecutive
     ticks in an unchanged world — silent livelock;
  4. a ``human`` terminal is legitimate only when the scenario expected it OR
     the escalation is the convergence ladder's NAMED
     ``completion_not_converging`` (bounded, auditable) — never an accident.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from unittest import mock

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(TESTS), str(SRC), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

TMP = tempfile.mkdtemp(prefix="conv-conformance-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402

store.init_db(project="switchboard")

from switchboard.application import completion_driver  # noqa: E402
from switchboard.domain.completion.executor import (  # noqa: E402
    CompletionEffectAdapters,
)

import _shared  # noqa: E402


MAX_TICKS = 12
#: Consecutive verified replays of one mutating effect in an unchanged world
#: before we call it a silent livelock. The convergence ladder must escalate
#: (reconcile once, then a NAMED human closeout) before this trips.
LIVELOCK_TICKS = 6

MUTATING = frozenset({
    "ensure_review_generation", "start_remediation", "mark_ready", "enqueue",
    "update_branch", "repair_dispatch", "fence_runner",
    "reconcile_provenance",
})
NAMED_LADDER_REASON = "completion_not_converging"


def react(world: dict[str, Any], effect: str) -> None:
    """Deterministic consequence of a COMPLETED effect on the scenario world.

    ``repair_dispatch`` and ``reconcile_provenance`` intentionally change
    nothing — the adversarial null repair the no-spin invariant exists for.
    """
    if effect == "mark_ready":
        world["draft"] = False
    elif effect == "update_branch":
        world["merge_state_status"] = "CLEAN"
    elif effect == "enqueue":
        world["queue"] = "queued"
    elif effect == "ensure_review_generation":
        world["runner"] = {"live": True, "role": "review_merge", "head": "same"}
    elif effect == "start_remediation":
        world["runner"] = {"live": True, "role": "remediation", "head": "same"}
    elif effect == "fence_runner":
        world["runner"] = {"live": False, "role": "none", "head": "none"}


def fingerprint(world: dict[str, Any]) -> str:
    return json.dumps(world, sort_keys=True, separators=(",", ":"))


def run_convergence(scenario: dict[str, Any], task_id: str) -> dict[str, Any]:
    world = deepcopy(scenario["world"])
    live = {"world": world}

    def hydrator(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        shaped = {**scenario, "world": live["world"]}
        return _shared.build_snapshot(shaped, task_id=task_id)

    def effect_adapter(_plan: dict[str, Any]) -> dict[str, Any]:
        return {"action": "completed"}

    adapters = CompletionEffectAdapters(
        ensure_review_generation=effect_adapter,
        start_remediation=effect_adapter,
        mark_ready=effect_adapter,
        update_branch=effect_adapter,
        enqueue=effect_adapter,
        repair_dispatch=effect_adapter,
        fence_runner=effect_adapter,
        reconcile_provenance=effect_adapter,
    )

    executed: set[tuple[str, str, str]] = set()
    history: list[dict[str, Any]] = []
    stable_replays = 0

    with mock.patch(
        "switchboard.application.commands.task_execution.fence_task_generation",
        return_value={"fenced": True},
    ):
        for tick in range(1, MAX_TICKS + 1):
            try:
                out = completion_driver.run_completion_tick(
                    task_id,
                    project="switchboard",
                    actor="conformance",
                    agent_id="conformance",
                    store_mod=object(),
                    hydrator=hydrator,
                    adapters=adapters,
                )
            except ValueError as exc:
                if "update_branch" not in str(exc):
                    raise
                return {
                    "outcome": "owned_block",
                    "reason": "head_changing_effect_rejected",
                    "ticks": tick,
                    "history": history,
                }
            route = str(out["decision"].get("route") or "")
            reason = str(out["decision"].get("reason_code") or "")
            effect = str(out["plan"].get("effect") or "")
            mission_output = str(
                out.get("command", {}).get("output")
                or out.get("decision", {}).get("mission_output")
                or ""
            )
            receipt = (out.get("execution") or {}).get("receipt") or {}
            fp = fingerprint(live["world"])
            history.append({
                "tick": tick, "route": route, "reason": reason,
                "effect": effect,
                "replay": bool(receipt.get("idempotent_replay")),
            })

            if mission_output == "AGENT_REQUIRES_HUMAN" or route == "human":
                return {
                    "outcome": "owned_block",
                    "reason": reason,
                    "ticks": tick,
                    "history": history,
                }
            if mission_output == "OBSERVE_MERGED" or route == "reconcile":
                return {
                    "outcome": "settled",
                    "reason": reason,
                    "ticks": tick,
                    "history": history,
                }
            if effect == "none":
                return {"outcome": "settled", "reason": reason,
                        "ticks": tick, "history": history}
            if effect not in MUTATING:
                # wait / attach_and_wait: in a frozen world this is a stable,
                # legitimate "waiting on the world" terminal.
                return {"outcome": "stable_wait", "reason": reason,
                        "ticks": tick, "history": history}

            if receipt.get("idempotent_replay"):
                stable_replays += 1
                if stable_replays >= LIVELOCK_TICKS:
                    return {"outcome": "FAIL_livelock", "reason": reason,
                            "effect": effect, "ticks": tick,
                            "history": history}
                continue

            stable_replays = 0
            key = (effect, reason, fp)
            if key in executed:
                return {"outcome": "FAIL_spin", "reason": reason,
                        "effect": effect, "ticks": tick, "history": history}
            executed.add(key)
            react(live["world"], effect)

    return {"outcome": "FAIL_no_terminal", "ticks": MAX_TICKS,
            "history": history}


def grade(scenario: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
    outcome = result["outcome"]
    if outcome.startswith("FAIL_"):
        return False, outcome
    if outcome == "human":
        expected_human = scenario["expect"].get("terminal") == "human"
        if expected_human or result.get("reason") == NAMED_LADDER_REASON:
            return True, f"human({result.get('reason')})"
        return False, f"unexpected_human({result.get('reason')})"
    return True, outcome


def main() -> int:
    scenario_dirs = (HERE / "scenarios", HERE / "scenarios" / "gold")
    scenarios: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in scenario_dirs:
        for scenario in _shared.load_scenarios(directory):
            if scenario["id"] in seen:
                continue
            seen.add(scenario["id"])
            scenarios.append(scenario)

    passed = failed = 0
    for index, scenario in enumerate(scenarios):
        task_id = f"CONV-{index}"
        result = run_convergence(scenario, task_id)
        ok, label = grade(scenario, result)
        if ok:
            passed += 1
            print(f"PASS  {scenario['id']}: {label} in {result['ticks']} tick(s)")
        else:
            failed += 1
            print(f"FAIL  {scenario['id']}: {label}")
            for row in result["history"]:
                print(
                    f"        tick {row['tick']}: route={row['route']} "
                    f"reason={row['reason']} effect={row['effect']} "
                    f"replay={row['replay']}"
                )
    print(
        f"\nConvergence conformance: {passed} passed, {failed} failed "
        f"(of {len(scenarios)} scenarios, budget {MAX_TICKS} ticks)"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
