#!/usr/bin/env python3
"""Tier-1 completion conformance: scenario -> production tick -> coverage."""
from __future__ import annotations

from contextlib import ExitStack
from itertools import product
import json
import os
from pathlib import Path
import sys
from typing import Any
from unittest.mock import patch

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
SRC = ROOT / "src"
for entry in (str(TESTS), str(SRC), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from switchboard.application import completion_driver  # noqa: E402
from switchboard.domain.completion.executor import (  # noqa: E402
    CompletionEffectAdapters,
)
from switchboard.domain.completion.state_machine import (  # noqa: E402
    build_completion_snapshot,
)


SCHEMA = "switchboard.completion_conformance.scenario.v1"
HERE = Path(__file__).resolve().parent
SCENARIO_DIR = HERE / "scenarios"
HEAD = "c" * 40
PR_NUMBER = 650
PR_URL = (
    "https://github.com/6th-Element-Labs/projectplanner/pull/"
    f"{PR_NUMBER}"
)

AXES = {
    "draft": (False, True),
    "ci": ("pass", "fail", "pending", "missing", "error"),
    "mergeability": (True, False, None),
    "review": ("passed", "missing", "changes_requested"),
    "queue": ("none", "queued", "unmergeable"),
    "runner": ("none", "review_merge", "remediation"),
}

CI_STATE = {
    "pass": "SUCCESS",
    "fail": "FAILURE",
    "pending": "IN_PROGRESS",
    "error": "ERROR",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _validate_scenario(value: Any, source: Path) -> dict[str, Any]:
    """Validate the dependency-free subset needed by the executable CI gate."""
    _require(isinstance(value, dict), f"{source}: scenario must be an object")
    _require(value.get("schema") == SCHEMA, f"{source}: invalid schema")
    _require(
        set(value) == {"schema", "id", "expect", "world", "timing"},
        f"{source}: unexpected or missing top-level fields",
    )
    expect = value.get("expect")
    world = value.get("world")
    timing = value.get("timing")
    _require(isinstance(expect, dict), f"{source}: expect must be an object")
    _require(isinstance(world, dict), f"{source}: world must be an object")
    _require(isinstance(timing, dict), f"{source}: timing must be an object")
    _require(
        expect.get("terminal") in {"merged", "blocked", "human", "reconcile_done"},
        f"{source}: invalid expect.terminal",
    )
    _require(
        isinstance(expect.get("reason_code"), str)
        and bool(expect["reason_code"]),
        f"{source}: expect.reason_code is required",
    )
    _require(
        isinstance(expect.get("role_sequence"), list),
        f"{source}: expect.role_sequence must be an array",
    )
    _require(world.get("draft") in AXES["draft"], f"{source}: invalid draft")
    _require(world.get("ci") in AXES["ci"], f"{source}: invalid ci")
    _require(
        world.get("mergeable") in AXES["mergeability"],
        f"{source}: invalid mergeable",
    )
    _require(world.get("review") in AXES["review"], f"{source}: invalid review")
    _require(world.get("queue") in AXES["queue"], f"{source}: invalid queue")
    runner = world.get("runner")
    _require(isinstance(runner, dict), f"{source}: runner must be an object")
    _require(
        runner.get("role") in {"none", "review_merge", "remediation"},
        f"{source}: invalid runner.role",
    )
    return value


def load_scenarios() -> list[dict[str, Any]]:
    paths = sorted(SCENARIO_DIR.glob("*.json"))
    _require(bool(paths), f"no scenarios found under {SCENARIO_DIR}")
    scenarios = [
        _validate_scenario(json.loads(path.read_text(encoding="utf-8")), path)
        for path in paths
    ]
    ids = [scenario["id"] for scenario in scenarios]
    _require(len(ids) == len(set(ids)), "scenario ids must be unique")
    return scenarios


def _snapshot(scenario: dict[str, Any]) -> dict[str, Any]:
    world = scenario["world"]
    ci = world["ci"]
    contexts = []
    if ci != "missing":
        contexts.append({
            "context": world["ci_context"],
            "state": CI_STATE[ci],
            "failure_attribution": world["ci_attribution"],
        })
    review = {
        "status": "" if world["review"] == "missing" else world["review"],
        "head_sha": HEAD,
        "number": PR_NUMBER,
        "pr_url": PR_URL,
    }
    queue = {}
    if world["queue"] == "queued":
        queue = {"state": "AWAITING_CHECKS"}
    elif world["queue"] == "unmergeable":
        queue = {
            "state": "UNMERGEABLE",
            "failure_attribution": world["ci_attribution"],
        }
    runner_world = world["runner"]
    runner = {"live": False}
    if runner_world["live"]:
        runner = {
            "live": True,
            "runner_session_id": f"runner-{scenario['id']}",
            "execution_id": f"execution-{scenario['id']}",
            "execution_connection_id": f"connection-{scenario['id']}",
            "generation": 1,
            "fence_epoch": 1,
            "role": runner_world["role"],
            "head_sha": HEAD if runner_world["head"] == "same" else "d" * 40,
        }
    return build_completion_snapshot(
        task={
            "task_id": "COORD-65",
            "status": "In Review",
            "git_state": {
                "head_sha": HEAD,
                "pr_number": PR_NUMBER,
                "pr_url": PR_URL,
            },
        },
        github_pr={
            "number": PR_NUMBER,
            "state": "OPEN",
            "draft": world["draft"],
            "url": PR_URL,
            "mergeable": world["mergeable"],
            "mergeStateStatus": world["merge_state_status"],
            "head": {"sha": HEAD},
        },
        required_status_contexts=[world["ci_context"]],
        status_contexts=contexts,
        review=review,
        merge_gate={"findings": []},
        merge_queue=queue,
        runner=runner,
    )


class _EffectLedger:
    """Minimal authoritative ledger double proving verified replay semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def claim(
        self,
        _effect_type: str,
        _resource_id: str,
        _operation: str,
        _payload: dict[str, Any],
        *,
        idem_key: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if idem_key in self.rows:
            row = self.rows[idem_key]
            return {
                "claimed": False,
                "verified": True,
                "effect_key": row["effect_key"],
                "proof": row["proof"],
            }
        effect_key = f"fixture-effect-{len(self.rows) + 1}"
        self.rows[idem_key] = {"effect_key": effect_key, "proof": {}}
        return {"claimed": True, "effect_key": effect_key}

    def verify(
        self,
        effect_key: str,
        *,
        readback: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        for row in self.rows.values():
            if row["effect_key"] == effect_key:
                row["proof"] = dict(readback)
                return {"effect_key": effect_key}
        raise AssertionError(f"unknown effect key {effect_key}")


def _terminal(result: dict[str, Any]) -> str:
    route = result["decision"]["route"]
    if route == "human":
        return "human"
    if route == "reconcile":
        return "reconcile_done"
    if result["decision"]["board_projection"] == "Blocked":
        return "blocked"
    if route in {"remediation", "coordination_retry", "wait"}:
        return "blocked"
    if (
        route == "review_merge"
        and result["plan"]["effect"] in {"enqueue", "attach_and_wait"}
    ):
        return "merged"
    return "blocked"


def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    ledger = _EffectLedger()
    run = {
        "run_id": f"fixture-run-{scenario['id']}",
        "state_version": 1,
        "attempt": 0,
    }

    def effect(plan: dict[str, Any]) -> dict[str, Any]:
        calls.append(dict(plan))
        return {"action": "completed", "scenario_id": scenario["id"]}

    adapters = CompletionEffectAdapters(
        ensure_review_generation=effect,
        start_remediation=effect,
        mark_ready=effect,
        enqueue=effect,
        requeue_merge_group=effect,
        repair_dispatch=effect,
        fence_runner=effect,
        reconcile_provenance=effect,
    )
    snapshot = _snapshot(scenario)
    with ExitStack() as stack:
        stack.enter_context(patch(
            "switchboard.storage.repositories.completion_runs."
            "get_active_completion_run",
            return_value=run,
        ))
        stack.enter_context(patch(
            "switchboard.application.completion_driver.ensure_completion_run",
            return_value=run,
        ))
        stack.enter_context(patch(
            "switchboard.domain.completion.executor._persist_run",
            return_value=run,
        ))
        stack.enter_context(patch(
            "switchboard.storage.repositories.decision_records."
            "record_decision_episode",
            return_value={"record_id": f"record-{scenario['id']}", "tick_count": 1},
        ))
        stack.enter_context(patch(
            "switchboard.storage.repositories.external_effects."
            "claim_external_effect",
            side_effect=ledger.claim,
        ))
        stack.enter_context(patch(
            "switchboard.storage.repositories.external_effects."
            "verify_external_effect",
            side_effect=ledger.verify,
        ))
        stack.enter_context(patch(
            "switchboard.application.commands.task_execution."
            "fence_task_generation",
            return_value={"fenced": True},
        ))
        first = completion_driver.run_completion_tick(
            "COORD-65",
            project="switchboard",
            actor="conformance",
            agent_id="conformance",
            store_mod=object(),
            hydrator=lambda *_args, **_kwargs: snapshot,
            adapters=adapters,
        )
        second = completion_driver.run_completion_tick(
            "COORD-65",
            project="switchboard",
            actor="conformance",
            agent_id="conformance",
            store_mod=object(),
            hydrator=lambda *_args, **_kwargs: snapshot,
            adapters=adapters,
        )

    expect = scenario["expect"]
    if os.environ.get("CONFORMANCE_PROVE_FAILURE") == scenario["id"]:
        expect = {**expect, "reason_code": "intentional_failure_proof"}
    actual_roles = (
        [first["decision"]["desired_role"]]
        if first["decision"].get("desired_role")
        else []
    )
    _require(
        _terminal(first) == expect["terminal"],
        f"{scenario['id']}: terminal {_terminal(first)!r} != {expect['terminal']!r}",
    )
    _require(
        first["decision"]["reason_code"] == expect["reason_code"],
        f"{scenario['id']}: reason {first['decision']['reason_code']!r} "
        f"!= {expect['reason_code']!r}",
    )
    _require(
        actual_roles == expect["role_sequence"],
        f"{scenario['id']}: roles {actual_roles!r} != {expect['role_sequence']!r}",
    )
    _require(
        first["decision"]["route"] == expect["route"],
        f"{scenario['id']}: route mismatch",
    )
    _require(
        first["plan"]["effect"] == expect["effect"],
        f"{scenario['id']}: effect mismatch",
    )
    _require(
        first["decision"] == second["decision"],
        f"{scenario['id']}: second tick changed the decision",
    )
    _require(
        first["plan"]["idem_key"] == second["plan"]["idem_key"],
        f"{scenario['id']}: second tick changed effect identity",
    )
    _require(
        len(calls) == 1,
        f"{scenario['id']}: effect adapter fired {len(calls)} times",
    )
    _require(
        second["execution"]["receipt"].get("idempotent_replay") is True,
        f"{scenario['id']}: second tick was not a verified replay",
    )
    return {
        "scenario_id": scenario["id"],
        "terminal": _terminal(first),
        "route": first["decision"]["route"],
        "effect": first["plan"]["effect"],
        "reason_code": first["decision"]["reason_code"],
        "roles": actual_roles,
        "second_tick": "idempotent_replay",
    }


def coverage_report(scenarios: list[dict[str, Any]]) -> dict[str, int]:
    covered = {
        (
            scenario["world"]["draft"],
            scenario["world"]["ci"],
            scenario["world"]["mergeable"],
            scenario["world"]["review"],
            scenario["world"]["queue"],
            (
                scenario["world"]["runner"]["role"]
                if scenario["world"]["runner"]["live"]
                else "none"
            ),
        ): scenario["id"]
        for scenario in scenarios
    }
    total = undefined = 0
    for cell in product(*(AXES[name] for name in AXES)):
        total += 1
        scenario_id = covered.get(cell)
        status = (
            f"SCENARIO_DEFINED:{scenario_id}"
            if scenario_id
            else "NO_SCENARIO_DEFINED"
        )
        undefined += int(scenario_id is None)
        rendered = ",".join(
            f"{name}={value!r}" for name, value in zip(AXES, cell)
        )
        print(f"COVERAGE {rendered} status={status}")
    print(
        "COVERAGE SUMMARY "
        f"defined={len(covered)} undefined={undefined} total={total}"
    )
    return {"defined": len(covered), "undefined": undefined, "total": total}


def main() -> int:
    passed = failed = 0
    scenarios = load_scenarios()
    for scenario in scenarios:
        try:
            result = run_scenario(scenario)
        except Exception as exc:
            failed += 1
            print(f"FAIL  {scenario['id']}: {exc}")
        else:
            passed += 1
            print("PASS  " + json.dumps(result, sort_keys=True))
    coverage = coverage_report(scenarios)
    _require(
        coverage["undefined"] > 0,
        "coverage must expose undefined cells, not grade only known scenarios",
    )
    print(
        f"\nCompletion conformance: {passed} passed, {failed} failed; "
        f"{coverage['undefined']} undefined cells"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
