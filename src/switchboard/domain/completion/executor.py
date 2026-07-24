"""Execute exactly one planned completion effect, then stop.

The planner is pure. This module is the side-effect boundary: persist the
completion run projection, perform the one effect, and return a receipt that
duplicate ticks can replay.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from switchboard.domain.completion.effects import plan_effect
from switchboard.domain.completion.human_closeout import build_human_closeout_request
from switchboard.storage.repositories import attention as attention_store
from switchboard.storage.repositories import completion_runs


FenceFn = Callable[[Any], Any]
WakeFn = Callable[[Mapping[str, Any]], Any]


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _persist_run(
    *,
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    actor: str,
    project: str,
) -> dict[str, Any]:
    decision, snapshot, plan = _map(decision), _map(snapshot), _map(plan)
    return completion_runs.transition_completion_run(
        {
            "task_id": plan.get("task_id") or snapshot.get("task_id"),
            "pr_number": plan.get("pr_number") or snapshot.get("pr_number"),
            "head_sha": plan.get("head_sha") or snapshot.get("head_sha"),
            "state": decision.get("state") or "blocked",
            "route": plan.get("route") or decision.get("route"),
            "reason_code": plan.get("reason_code") or decision.get("reason_code"),
            "desired_role": plan.get("role") or decision.get("desired_role") or "",
            "board_status": (
                decision.get("board_projection")
                or plan.get("board_projection")
                or "In Review"
            ),
            "evidence_refs": {
                "decision": {
                    "route": plan.get("route"),
                    "reason_code": plan.get("reason_code"),
                    "idem_key": plan.get("idem_key"),
                    "head_sha": plan.get("head_sha"),
                },
                "ci": {"head_sha": plan.get("head_sha"), "status": "observed"},
                "review": _map(snapshot.get("review")),
            },
        },
        actor=actor,
        project=project,
    )


def _escalate_human(
    plan: Mapping[str, Any],
    *,
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    run: Mapping[str, Any],
    project: str,
    actor: str,
    fence_generation: Optional[FenceFn] = None,
) -> dict[str, Any]:
    persisted = _persist_run(
        decision=decision, snapshot=snapshot, plan=plan, actor=actor,
        project=project,
    )
    # Prefer the durable run identity for the frozen closeout context.
    closeout_run = {
        **_map(run),
        "run_id": persisted.get("run_id") or _map(run).get("run_id"),
        "state_version": persisted.get("state_version") or _map(run).get("state_version"),
        "attempt": persisted.get("attempt") or _map(run).get("attempt"),
    }
    # Rebuild the plan against the persisted run so the idempotency key matches
    # the durable state_version/attempt that operators will see on rehydrate.
    durable_plan = plan_effect(decision, snapshot, closeout_run)
    request_data = build_human_closeout_request(
        plan=durable_plan, decision=decision, snapshot=snapshot, run=closeout_run,
    )
    attention = attention_store.default_attention_repository.create_request(
        request_data, actor=actor, project=project,
    )
    fenced_generation = None
    # Terminalize the live generation once when the human closeout is first
    # persisted. Replays must not re-fence Watch/session evidence.
    if (
        attention.get("created")
        and durable_plan.get("fence_required")
        and fence_generation is not None
    ):
        fence_generation(durable_plan.get("fence_generation"))
        fenced_generation = durable_plan.get("fence_generation")
    return {
        "effect": "escalate_human",
        "route": "human",
        "run": persisted,
        "plan": durable_plan,
        "attention": attention,
        "fenced_generation": fenced_generation,
        "receipt": {
            "schema": "switchboard.completion_effect_receipt.v1",
            "effect": "escalate_human",
            "idem_key": durable_plan.get("idem_key"),
            "attention_request_id": attention["request"]["request_id"],
            "idempotent_replay": bool(attention.get("idempotent_replay")),
        },
    }


def execute_effect(
    plan: Mapping[str, Any],
    *,
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    run: Mapping[str, Any] | None = None,
    project: str,
    actor: str,
    fence_generation: Optional[FenceFn] = None,
    wake_completion_owner: Optional[WakeFn] = None,
) -> dict[str, Any]:
    """Perform the single planned effect for this tick."""
    del wake_completion_owner  # used by resume helpers; tick execution stays one-effect
    plan = _map(plan)
    effect = str(plan.get("effect") or "")
    if effect == "escalate_human":
        return _escalate_human(
            plan,
            decision=decision,
            snapshot=snapshot,
            run=_map(run),
            project=project,
            actor=actor,
            fence_generation=fence_generation,
        )
    if effect in {"wait", "none", "attach_and_wait"}:
        persisted = _persist_run(
            decision=decision, snapshot=snapshot, plan=plan, actor=actor,
            project=project,
        )
        return {
            "effect": effect,
            "route": plan.get("route"),
            "run": persisted,
            "plan": plan,
            "receipt": {
                "schema": "switchboard.completion_effect_receipt.v1",
                "effect": effect,
                "idem_key": plan.get("idem_key"),
            },
        }
    raise NotImplementedError(f"completion effect not implemented: {effect}")


def resume_after_human_decision(
    decided: Mapping[str, Any],
    *,
    project: str,
    actor: str,
    wake_completion_owner: Optional[WakeFn] = None,
) -> dict[str, Any]:
    """Wake the completion owner after an authorized decision.

    The UI must not claim Resumed until a delivery/execution receipt exists.
    """
    del project, actor
    decided = _map(decided)
    request = _map(decided.get("request"))
    context = _map(request.get("context"))
    payload = {
        "task_id": request.get("task_id") or context.get("task_id"),
        "request_id": request.get("request_id"),
        "completion_run_id": context.get("completion_run_id"),
        "state_version": context.get("state_version"),
        "head_sha": context.get("head_sha"),
        "reason_code": context.get("reason_code"),
        "action": "rehydrate_and_classify",
    }
    if wake_completion_owner is not None:
        wake_completion_owner(payload)
    return {
        "status": request.get("status") or "decision_recorded",
        "resumed": False,
        "wake": payload,
        "reason": "awaiting_delivery_or_execution_receipt",
    }


def mark_human_resume_receipt(
    request_id: str,
    *,
    expected_version: int,
    host_id: str,
    actor: str,
    receipt: Mapping[str, Any],
    project: str,
) -> dict[str, Any]:
    """Record the delivery/execution receipt that unlocks UI Resumed.

    Lifecycle is decision_recorded -> delivering -> resolved. The UI must not
    show Resumed until this receipt lands.
    """
    repo = attention_store.default_attention_repository
    current = repo.get_request(request_id, project=project)
    version = int(expected_version or current.get("version") or 1)
    if current.get("status") == "decision_recorded":
        current = repo.transition(
            request_id,
            expected_version=version,
            target_status="delivering",
            actor=actor,
            delivery_claimed_by=host_id,
            project=project,
        )
        version = int(current.get("version") or version)
    transitioned = repo.transition(
        request_id,
        expected_version=version,
        target_status="resolved",
        actor=actor,
        delivery_receipt=dict(receipt or {}),
        delivery_claimed_by=host_id,
        project=project,
    )
    return {
        "status": transitioned.get("status"),
        "resumed": transitioned.get("status") == "resolved",
        "request": transitioned,
        "receipt": dict(receipt or {}),
    }


# Expose resume helpers as attributes for the test import style
# ``execute_effect.resume_after_human_decision``.
execute_effect.resume_after_human_decision = resume_after_human_decision  # type: ignore[attr-defined]
execute_effect.mark_human_resume_receipt = mark_human_resume_receipt  # type: ignore[attr-defined]


__all__ = [
    "execute_effect",
    "mark_human_resume_receipt",
    "resume_after_human_decision",
]
