"""Typed application commands for one normalized completion-controller result."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Optional

from switchboard.domain.completion.executor import (
    CompletionEffectAdapters,
    execute_effect,
)
from switchboard.domain.completion.normalization_law import NormalizedAction


_LEGACY_EFFECTS = {
    NormalizedAction.START: frozenset({
        "ensure_review_generation", "start_remediation",
    }),
    NormalizedAction.RETRY_CI: frozenset({
        "repair_dispatch", "retry_ci",
    }),
    NormalizedAction.MARK_READY: frozenset({"mark_ready"}),
    NormalizedAction.ARM_MERGE: frozenset({"enqueue"}),
    NormalizedAction.BLOCK: frozenset({
        "escalate_human", "fence_runner", "reconcile_provenance",
        "repair_dispatch",
    }),
    NormalizedAction.WAIT: frozenset({
        "wait", "none", "attach_and_wait",
    }),
    NormalizedAction.MERGED: frozenset({"reconcile_provenance"}),
}


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def execute_normalized_command(
    normalized: Mapping[str, Any],
    *,
    legacy_plan: Mapping[str, Any],
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    run: Mapping[str, Any] | None,
    project: str,
    actor: str,
    fence_generation: Any = None,
    adapters: Optional[CompletionEffectAdapters] = None,
) -> dict[str, Any]:
    """Execute at most one command selected by the versioned normalization table.

    The legacy plan remains an adapter payload during migration, but it no
    longer selects the command.  Its effect must agree with the normalized
    action or the tick fails closed.
    """
    normalized_map = _map(normalized)
    plan = _map(legacy_plan)
    plan["normalized_command"] = normalized_map
    command_decision = _map(decision)
    try:
        action = NormalizedAction(str(normalized_map.get("action") or ""))
    except ValueError as exc:
        raise ValueError("normalized completion action is unsupported") from exc
    effect = str(plan.get("effect") or "")
    if effect not in _LEGACY_EFFECTS[action]:
        raise ValueError(
            f"normalized action {action.value} conflicts with effect {effect!r}"
        )
    if action is NormalizedAction.BLOCK and effect in {
        "reconcile_provenance", "repair_dispatch",
    }:
        plan.update({
            "effect": "escalate_human",
            "route": "human",
            "reason_code": normalized_map.get("reason_code"),
            "idem_key": normalized_map.get("idempotency_key"),
        })
        command_decision.update({
            "state": "blocked",
            "route": "human",
            "reason_code": normalized_map.get("reason_code"),
            "board_projection": "Blocked",
            "desired_role": None,
        })
    elif (
        action is NormalizedAction.RETRY_CI
        and effect == "repair_dispatch"
        and str(normalized_map.get("reason_code") or "")
        == "required_ci_infrastructure_failure"
    ):
        plan["effect"] = "retry_ci"

    effect_adapters = adapters or CompletionEffectAdapters()
    if action is NormalizedAction.BLOCK and effect == "fence_runner":
        # BLOCK records an owned blocker; it does not borrow capacity-plane
        # authority to fence a runner. The completion owner may request the
        # appropriate lifecycle transition on a later fresh tick.
        effect_adapters = replace(
            effect_adapters,
            fence_runner=lambda _plan: {
                "action": "blocker_recorded",
                "owner": _map(normalized_map.get("block")).get("owner"),
            },
        )
    elif action is NormalizedAction.MERGED and effect == "reconcile_provenance":
        # MERGED is an observation of canonical provider provenance. The
        # webhook/reconciler owns Done, so this command must not invoke an
        # adapter that could synthesize or advance lifecycle state.
        effect_adapters = replace(
            effect_adapters,
            reconcile_provenance=lambda _plan: {
                "action": "canonical_provenance_observed",
                "head_sha": normalized_map.get("head_sha"),
            },
        )

    result = execute_effect(
        plan,
        decision=command_decision,
        snapshot=snapshot,
        run=run,
        project=project,
        actor=actor,
        fence_generation=fence_generation,
        adapters=effect_adapters,
    )
    result["command"] = {
        "schema": "switchboard.completion_application_command.v1",
        "action": action.value,
        "snapshot_id": normalized_map.get("snapshot_id"),
        "observed_at": normalized_map.get("observed_at"),
        "controller_build_sha": normalized_map.get("controller_build_sha"),
        "table_version": normalized_map.get("table_version"),
        "idempotency_key": normalized_map.get("idempotency_key"),
        "mutated": action not in {NormalizedAction.WAIT, NormalizedAction.MERGED},
    }
    return result


__all__ = ["execute_normalized_command"]
