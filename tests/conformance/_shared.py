"""Shared fixture-mode building blocks for T1 (fixture tick) and T2 (observe).

Both tiers drive the same production modules (``classify_completion``,
``plan_effect``, ``run_completion_tick``) from the same ``scenario.v1`` world.
This module holds the parts that would otherwise be duplicated between
``test_completion_conformance.py`` (T1) and ``test_observe_conformance.py``
(T2): scenario loading/validation, snapshot construction, the hermetic
storage-layer patches, and terminal classification.

Not a test itself (no ``test_`` prefix) — the CI test-file discovery in
``scripts/switchboard_ci.sh`` will not pick this up and run it standalone.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from itertools import product
import json
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

from switchboard.domain.completion.state_machine import build_completion_snapshot


SCHEMA = "switchboard.completion_conformance.scenario.v1"
HEAD = "c" * 40
PR_NUMBER = 650
PR_URL = f"https://github.com/6th-Element-Labs/projectplanner/pull/{PR_NUMBER}"

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


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def validate_scenario(value: Any, source: Any) -> dict[str, Any]:
    """Validate the dependency-free subset needed by the executable gates."""
    require(isinstance(value, dict), f"{source}: scenario must be an object")
    require(value.get("schema") == SCHEMA, f"{source}: invalid schema")
    require(
        set(value) == {"schema", "id", "expect", "world", "timing"},
        f"{source}: unexpected or missing top-level fields",
    )
    expect = value.get("expect")
    world = value.get("world")
    timing = value.get("timing")
    require(isinstance(expect, dict), f"{source}: expect must be an object")
    require(isinstance(world, dict), f"{source}: world must be an object")
    require(isinstance(timing, dict), f"{source}: timing must be an object")
    require(
        expect.get("terminal") in {"merged", "blocked", "human", "reconcile_done"},
        f"{source}: invalid expect.terminal",
    )
    require(
        isinstance(expect.get("reason_code"), str) and bool(expect["reason_code"]),
        f"{source}: expect.reason_code is required",
    )
    require(
        isinstance(expect.get("role_sequence"), list),
        f"{source}: expect.role_sequence must be an array",
    )
    require(world.get("draft") in AXES["draft"], f"{source}: invalid draft")
    require(world.get("ci") in AXES["ci"], f"{source}: invalid ci")
    require(
        world.get("mergeable") in AXES["mergeability"],
        f"{source}: invalid mergeable",
    )
    require(world.get("review") in AXES["review"], f"{source}: invalid review")
    require(world.get("queue") in AXES["queue"], f"{source}: invalid queue")
    runner = world.get("runner")
    require(isinstance(runner, dict), f"{source}: runner must be an object")
    require(
        runner.get("role") in {"none", "review_merge", "remediation"},
        f"{source}: invalid runner.role",
    )
    return value


def load_scenarios(scenario_dir: Path) -> list[dict[str, Any]]:
    paths = sorted(Path(scenario_dir).glob("*.json"))
    require(bool(paths), f"no scenarios found under {scenario_dir}")
    scenarios = [
        validate_scenario(json.loads(path.read_text(encoding="utf-8")), path)
        for path in paths
    ]
    ids = [scenario["id"] for scenario in scenarios]
    require(len(ids) == len(set(ids)), "scenario ids must be unique")
    return scenarios


def build_snapshot(
    scenario: dict[str, Any], *, task_id: str = "COORD-65",
) -> dict[str, Any]:
    """Turn one ``scenario.world`` into a ``build_completion_snapshot`` input.

    Shared by T1 (fixture tick) and T2 fixture-mode observe ticks — the same
    world must produce the same snapshot regardless of which tier reads it.
    """
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
    queue: dict[str, Any] = {}
    if world["queue"] == "queued":
        queue = {"state": "AWAITING_CHECKS"}
    elif world["queue"] == "unmergeable":
        queue = {
            "state": "UNMERGEABLE",
            "failure_attribution": world["ci_attribution"],
        }
    runner_world = world["runner"]
    runner: dict[str, Any] = {"live": False}
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
            "task_id": task_id,
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


class EffectLedger:
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
        self, effect_key: str, *, readback: dict[str, Any], **_kwargs: Any,
    ) -> dict[str, Any]:
        for row in self.rows.values():
            if row["effect_key"] == effect_key:
                row["proof"] = dict(readback)
                return {"effect_key": effect_key}
        raise AssertionError(f"unknown effect key {effect_key}")


def terminal_for(result: dict[str, Any]) -> str:
    """Collapse one completion-tick result to the scenario's terminal vocabulary."""
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


@contextmanager
def hermetic_completion_patches(
    run: dict[str, Any], ledger: EffectLedger,
) -> Iterator[None]:
    """Patch every storage-layer seam ``run_completion_tick`` touches.

    Used by both T1's ``run_scenario`` and T2's fixture-mode observe tick so a
    scenario tick never opens a real database connection or reaches `gh`.
    """
    with ExitStack() as stack:
        stack.enter_context(patch(
            "switchboard.storage.repositories.completion_runs."
            "get_active_completion_run",
            return_value=run,
        ))
        # COORD-77: the executor counts stable replays for the convergence
        # ladder on the verified-replay path; hermetic ticks must not open a DB.
        stack.enter_context(patch(
            "switchboard.storage.repositories.completion_runs."
            "note_stable_replay",
            return_value=1,
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
            return_value={
                "record_id": f"record-{run.get('run_id')}", "tick_count": 1,
            },
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
        yield


def coverage_cells() -> Any:
    """Every axis cell the T1/T2 truth table should eventually define."""
    return product(*(AXES[name] for name in AXES))


__all__ = [
    "SCHEMA", "HEAD", "PR_NUMBER", "PR_URL", "AXES", "CI_STATE",
    "require", "validate_scenario", "load_scenarios", "build_snapshot",
    "EffectLedger", "terminal_for", "hermetic_completion_patches",
    "coverage_cells",
]
