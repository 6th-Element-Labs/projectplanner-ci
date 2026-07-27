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
import sys
from typing import Any, Iterator
from unittest.mock import patch

from switchboard.domain.completion.state_machine import build_completion_snapshot

# This module is imported both flat (``import _shared``, from T1 and
# _gold_shared) and as a package module (``from tests.conformance import
# _shared``, from scripts/completion_conformance). Bootstrap our own directory
# so the sibling import below resolves either way, exactly as _gold_shared does.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _evidence  # noqa: E402


SCHEMA = "switchboard.completion_conformance.scenario.v1"
HEAD = "c" * 40
PR_NUMBER = 650
PR_URL = f"https://github.com/6th-Element-Labs/projectplanner/pull/{PR_NUMBER}"

#: Coverage product axes — keep this set stable so T1's undefined-cell report
#: stays readable. Extended GitHub values are accepted by validate/build but
#: are not multiplied into every coverage cell.
AXES = {
    "draft": (False, True),
    "ci": ("pass", "fail", "pending", "missing", "error"),
    "mergeability": (True, False, None),
    "review": ("passed", "missing", "changes_requested"),
    "queue": ("none", "queued", "unmergeable", "ejected"),
    "runner": ("none", "review_merge", "remediation"),
}

CI_VALUES = (
    "pass", "fail", "pending", "missing", "error",
    "timed_out", "action_required", "cancelled", "stale", "startup_failure",
)
QUEUE_VALUES = ("none", "queued", "unmergeable", "ejected", "locked")
PR_STATE_VALUES = ("open", "merged", "closed")
REVIEW_FINDINGS_VALUES = ("none", "automatic", "judgment")

CI_STATE = {
    "pass": "SUCCESS",
    "fail": "FAILURE",
    "pending": "IN_PROGRESS",
    "error": "ERROR",
    "timed_out": "TIMED_OUT",
    "action_required": "ACTION_REQUIRED",
    "cancelled": "CANCELLED",
    "stale": "STALE",
    "startup_failure": "STARTUP_FAILURE",
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
    near_miss = expect.get("missing_artifact_near_miss_keys")
    require(
        near_miss is None or (
            isinstance(near_miss, list)
            and bool(near_miss)
            and all(isinstance(item, str) and item for item in near_miss)
        ),
        f"{source}: expect.missing_artifact_near_miss_keys must be a non-empty "
        "array of strings when present",
    )
    require(world.get("draft") in AXES["draft"], f"{source}: invalid draft")
    require(world.get("ci") in CI_VALUES, f"{source}: invalid ci")
    require(
        world.get("mergeable") in AXES["mergeability"],
        f"{source}: invalid mergeable",
    )
    require(world.get("review") in AXES["review"], f"{source}: invalid review")
    require(world.get("queue") in QUEUE_VALUES, f"{source}: invalid queue")
    removal = world.get("queue_removal_reason")
    if removal is not None:
        require(
            world.get("queue") == "ejected",
            f"{source}: queue_removal_reason requires queue=ejected",
        )
        require(
            removal in {"failed_checks", "checks_timed_out"},
            f"{source}: invalid queue_removal_reason",
        )
    also_pending = world.get("also_pending_contexts")
    if also_pending is not None:
        require(
            isinstance(also_pending, list)
            and all(isinstance(item, str) and item.strip() for item in also_pending),
            f"{source}: also_pending_contexts must be a list of non-empty strings",
        )
    pr_state = world.get("pr_state", "open")
    require(pr_state in PR_STATE_VALUES, f"{source}: invalid pr_state")
    if world.get("reopen_authorized") is not None:
        require(
            isinstance(world.get("reopen_authorized"), bool),
            f"{source}: reopen_authorized must be a boolean",
        )
        require(
            pr_state == "closed",
            f"{source}: reopen_authorized requires pr_state=closed",
        )
    if world.get("review_retry_exhausted") is not None:
        require(
            isinstance(world.get("review_retry_exhausted"), bool),
            f"{source}: review_retry_exhausted must be a boolean",
        )
    if world.get("queue_retry_exhausted") is not None:
        require(
            isinstance(world.get("queue_retry_exhausted"), bool),
            f"{source}: queue_retry_exhausted must be a boolean",
        )
        require(
            world.get("queue") == "locked",
            f"{source}: queue_retry_exhausted requires queue=locked",
        )
    findings_class = world.get("review_findings", "none")
    require(
        findings_class in REVIEW_FINDINGS_VALUES,
        f"{source}: invalid review_findings",
    )
    if findings_class != "none":
        require(
            world.get("review") == "changes_requested",
            f"{source}: review_findings requires review=changes_requested",
        )
    runner = world.get("runner")
    require(isinstance(runner, dict), f"{source}: runner must be an object")
    require(
        runner.get("role") in {"none", "review_merge", "remediation"},
        f"{source}: invalid runner.role",
    )
    # COORD-78: the board-evidence axis. Optional, because a scenario that
    # declares no evidence world is asserting the derived default one
    # (_evidence.default_evidence) — not "evidence is unmodelled".
    _evidence.validate_evidence(world.get("evidence"), source, require)
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
    also_pending = [
        str(name).strip()
        for name in list(world.get("also_pending_contexts") or [])
        if str(name).strip()
    ]
    for name in also_pending:
        contexts.append({
            "context": name,
            "state": CI_STATE["pending"],
            "failure_attribution": world["ci_attribution"],
        })
    required_contexts = [world["ci_context"], *also_pending]
    review = {
        "status": "" if world["review"] == "missing" else world["review"],
        "head_sha": HEAD,
        "number": PR_NUMBER,
        "pr_url": PR_URL,
    }
    if world.get("review_retry_exhausted") is True:
        review["retry_exhausted"] = True
    findings_class = world.get("review_findings", "none")
    if findings_class == "automatic":
        review["findings"] = [
            {"finding_class": "automatic", "code": "style_violation"},
        ]
    elif findings_class == "judgment":
        review["findings"] = [
            {"finding_class": "judgment", "code": "design_concern"},
        ]
    queue: dict[str, Any] = {}
    if world["queue"] == "queued":
        queue = {"state": "AWAITING_CHECKS"}
    elif world["queue"] == "unmergeable":
        queue = {
            "state": "UNMERGEABLE",
            "failure_attribution": world["ci_attribution"],
        }
    elif world["queue"] == "locked":
        queue = {"state": "LOCKED"}
        if world.get("queue_retry_exhausted") is True:
            queue["retry_exhausted"] = True
    elif world["queue"] == "ejected":
        # Not currently in the queue (no mergeQueueEntry), but completion already
        # verified an enqueue for this exact head and GitHub later removed it.
        # Tip gates remain green — this is the BREAKDOWN 38 / 40 jam shape.
        queue = {
            "prior_enqueue_verified": True,
            "prior_enqueue_effect_key": f"effect-enqueue-{scenario['id']}",
            "verified_queue_effect_count": 1,
            "last_removal_reason": (
                world.get("queue_removal_reason") or "failed_checks"
            ),
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
    pr_state = str(world.get("pr_state") or "open").upper()
    github_pr: dict[str, Any] = {
        "number": PR_NUMBER,
        "state": pr_state,
        "draft": world["draft"],
        "url": PR_URL,
        "mergeable": world["mergeable"],
        "mergeStateStatus": world["merge_state_status"],
        "head": {"sha": HEAD},
    }
    if pr_state == "MERGED":
        github_pr["merged"] = True
    if pr_state == "CLOSED" and world.get("reopen_authorized") is True:
        github_pr["reopen_authorized"] = True
    # COORD-78: this used to be `merge_gate={"findings": []}`. A hardcoded empty
    # finding list made the whole evidence family — the family the last three
    # completion incidents came from — unrepresentable, and it asserted a fact
    # about production (that the gate has nothing to say) rather than reading
    # one. The findings below are whatever the REAL merge gate returns for this
    # world; see tests/conformance/_evidence.py for why they are never
    # hand-built. The gate is handed the same PR object and the same required
    # contexts the snapshot gets, so the two views of one world can never
    # disagree for a reason the scenario did not declare.
    merge_gate = _evidence.merge_gate_result(
        scenario["id"], world,
        task_id=task_id,
        head_sha=HEAD,
        pr_url=PR_URL,
        pr_number=PR_NUMBER,
        github_pr=github_pr,
        required_status_contexts=required_contexts,
        status_contexts=contexts,
    )
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
        github_pr=github_pr,
        required_status_contexts=required_contexts,
        status_contexts=contexts,
        review=review,
        merge_gate=merge_gate,
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
    effect = result["plan"]["effect"]
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
        and effect in {"enqueue", "attach_and_wait"}
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


#: The board-evidence axes, re-exported so callers do not have to know whether
#: an axis lives in the PR world or the evidence world. ``AXES`` stays the PR
#: world alone: it is the key space of the ``coverage_cells`` cartesian product,
#: and crossing four evidence axes into it would produce ~243,000 cells.
#: Evidence coverage is reported per axis by ``_evidence.evidence_coverage``.
#: Re-exported in full so a caller never needs to import ``_evidence``
#: separately — it caches an in-memory database, and a second module instance
#: (flat vs ``tests.conformance``) would quietly build a second one.
EVIDENCE_AXES = _evidence.EVIDENCE_AXES
default_evidence = _evidence.default_evidence
effective_evidence = _evidence.effective_evidence
evidence_coverage = _evidence.evidence_coverage
missing_artifact_near_miss_keys = _evidence.missing_artifact_near_miss_keys


__all__ = [
    "SCHEMA", "HEAD", "PR_NUMBER", "PR_URL", "AXES", "CI_STATE",
    "CI_VALUES", "QUEUE_VALUES", "PR_STATE_VALUES", "REVIEW_FINDINGS_VALUES",
    "EVIDENCE_AXES", "require", "validate_scenario", "load_scenarios",
    "build_snapshot", "EffectLedger", "terminal_for",
    "hermetic_completion_patches", "coverage_cells", "default_evidence",
    "effective_evidence", "evidence_coverage",
    "missing_artifact_near_miss_keys",
]
