"""Typed application commands for one normalized completion-controller result."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from switchboard.domain.completion.executor import (
    CompletionEffectAdapters,
    execute_effect,
)
from switchboard.domain.completion.normalization_law import NormalizedAction


_COMMAND_EFFECTS = {
    NormalizedAction.START: frozenset({
        "ensure_review_generation",
        "start_remediation",
        "start_implementation",
    }),
    NormalizedAction.RETRY_CI: frozenset({"retry_ci"}),
    NormalizedAction.MARK_READY: frozenset({"mark_ready"}),
    NormalizedAction.ARM_MERGE: frozenset({"enqueue"}),
    NormalizedAction.BLOCK: frozenset({
        "escalate_human", "agent_requires_human",
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
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    run: Mapping[str, Any] | None,
    project: str,
    actor: str,
    adapters: Optional[CompletionEffectAdapters] = None,
) -> dict[str, Any]:
    """Execute at most one command selected by the versioned normalization table.

    The normalized command is the sole production authority.
    """
    normalized_map = _map(normalized)
    command = _map(normalized_map.get("command"))
    if not command:
        raise ValueError("normalized completion command is required")
    plan = command
    plan["normalized_command"] = normalized_map
    command_decision = _map(decision)
    try:
        action = NormalizedAction(str(normalized_map.get("action") or ""))
    except ValueError as exc:
        raise ValueError("normalized completion action is unsupported") from exc
    effect = str(plan.get("effect") or "")
    if effect not in _COMMAND_EFFECTS[action]:
        raise ValueError(
            f"normalized action {action.value} conflicts with effect {effect!r}"
        )
    effect_adapters = adapters or CompletionEffectAdapters()
    if action is NormalizedAction.BLOCK:
        command_decision.update({
            "state": "blocked",
            "route": "human",
            "reason_code": normalized_map.get("reason_code"),
            "board_projection": "Blocked",
            "desired_role": None,
        })
    elif action is NormalizedAction.MERGED:
        # MERGED is an observation of canonical provider provenance. The
        # webhook/reconciler owns Done, so no adapter is invoked.
        return {
            "effect": "reconcile_provenance",
            "route": plan.get("route"),
            "run": _map(run),
            "plan": plan,
            "result": {
                "action": "canonical_provenance_observed",
                "head_sha": normalized_map.get("head_sha"),
            },
            "receipt": {
                "schema": "switchboard.completion_effect_receipt.v1",
                "effect": "reconcile_provenance",
                "idem_key": normalized_map.get("idempotency_key"),
                "verified": True,
                "pending": False,
                "observed": True,
            },
            "command": {
                "schema": "switchboard.completion_application_command.v1",
                "action": action.value,
                "snapshot_id": normalized_map.get("snapshot_id"),
                "observed_at": normalized_map.get("observed_at"),
                "controller_build_sha": normalized_map.get(
                    "controller_build_sha"
                ),
                "table_version": normalized_map.get("table_version"),
                "idempotency_key": normalized_map.get("idempotency_key"),
                "mutated": False,
            },
        }

    result = execute_effect(
        plan,
        decision=command_decision,
        snapshot=snapshot,
        run=run,
        project=project,
        actor=actor,
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
