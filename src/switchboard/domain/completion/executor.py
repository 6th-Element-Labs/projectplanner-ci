"""Execute exactly one planned completion effect, then stop.

The planner is pure. This module is the side-effect boundary: persist the
completion run projection, perform the one effect, and return a receipt that
duplicate ticks can replay.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from switchboard.domain.completion.effects import plan_effect
from switchboard.domain.completion.human_closeout import build_human_closeout_request


FenceFn = Callable[[Any], Any]
WakeFn = Callable[[Mapping[str, Any]], Any]
EffectFn = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class CompletionEffectAdapters:
    """Concrete side-effect ports owned by the production completion driver."""

    ensure_review_generation: Optional[EffectFn] = None
    start_remediation: Optional[EffectFn] = None
    mark_ready: Optional[EffectFn] = None
    enqueue: Optional[EffectFn] = None
    requeue_merge_group: Optional[EffectFn] = None
    repair_dispatch: Optional[EffectFn] = None
    reconcile_provenance: Optional[EffectFn] = None

    def for_effect(self, effect: str) -> Optional[EffectFn]:
        return getattr(self, effect, None)


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
    # Lazy import keeps domain.completion importable during db.connection boot.
    from switchboard.storage.repositories import completion_runs

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
                "ci": {
                    "head_sha": plan.get("head_sha"),
                    "status": "observed",
                    "status_contexts": _map(snapshot.get("status_contexts")),
                },
                "review": _map(snapshot.get("review")),
                "merge_gate": _map(snapshot.get("merge_gate")),
                "work_session": _map(snapshot.get("work_session")),
                "runner": _map(snapshot.get("runner")),
                "acceptance_findings": list(
                    decision.get("acceptance_findings") or []),
                "escalated_findings": list(
                    decision.get("escalated_findings") or []),
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
    from switchboard.storage.repositories import attention as attention_store

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


def _effect_failed(result: Any) -> str:
    row = _map(result)
    if row.get("error") or row.get("refused"):
        return str(row.get("error") or row.get("reason") or "effect refused")
    if row.get("action") == "refused":
        return str(row.get("reason") or "effect refused")
    try:
        if int(row.get("returncode") or 0) != 0:
            return str(row.get("stderr") or f"returncode={row['returncode']}")
    except (TypeError, ValueError):
        return "invalid effect returncode"
    return ""


def _effect_pending(result: Any) -> bool:
    row = _map(result)
    return str(row.get("action") or row.get("status") or "").strip().lower() in {
        "transitioning", "pending", "stopping", "starting",
    }


def _execute_mutating_effect(
    effect: str,
    plan: Mapping[str, Any],
    *,
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    project: str,
    actor: str,
    adapters: CompletionEffectAdapters,
    fence_generation: Optional[FenceFn],
) -> dict[str, Any]:
    adapter = adapters.for_effect(effect)
    if adapter is None:
        raise NotImplementedError(f"completion effect adapter missing: {effect}")

    persisted = _persist_run(
        decision=decision, snapshot=snapshot, plan=plan, actor=actor,
        project=project,
    )
    from switchboard.storage.repositories import external_effects

    payload = {
        "idem_key": plan.get("idem_key"),
        "effect": effect,
        "route": plan.get("route"),
        "role": plan.get("role"),
        "head_sha": plan.get("head_sha"),
        "pr_number": plan.get("pr_number"),
        "acceptance_findings": list(plan.get("acceptance_findings") or []),
        "escalated_findings": list(plan.get("escalated_findings") or []),
    }
    ledger = external_effects.claim_external_effect(
        "completion_effect",
        str(plan.get("task_id") or ""),
        effect,
        payload,
        task_id=str(plan.get("task_id") or ""),
        idem_key=str(plan.get("idem_key") or ""),
        actor=actor,
        project=project,
    )
    if ledger.get("verified"):
        return {
            "effect": effect,
            "route": plan.get("route"),
            "run": persisted,
            "plan": dict(plan),
            "result": ledger.get("proof") or {},
            "receipt": {
                "schema": "switchboard.completion_effect_receipt.v1",
                "effect": effect,
                "idem_key": plan.get("idem_key"),
                "effect_key": ledger.get("effect_key"),
                "idempotent_replay": True,
            },
        }
    existing_effect = _map(ledger.get("effect"))
    if (
        not ledger.get("claimed")
        and existing_effect.get("status") == "claimed"
        and time.time() - float(existing_effect.get("updated_at") or time.time()) < 60
    ):
        # Another tick currently owns issuance. A stale claim is recoverable
        # after the bounded window; a fresh one must never double-fire.
        return {
            "effect": effect,
            "route": plan.get("route"),
            "run": persisted,
            "plan": dict(plan),
            "result": existing_effect.get("readback") or {},
            "receipt": {
                "schema": "switchboard.completion_effect_receipt.v1",
                "effect": effect,
                "idem_key": plan.get("idem_key"),
                "effect_key": ledger.get("effect_key"),
                "verified": False,
                "pending": True,
                "idempotent_replay": True,
                "reason": "effect_claim_in_flight",
            },
        }

    fenced_generation = None
    if plan.get("fence_required") and fence_generation is not None:
        fence_generation(plan.get("fence_generation"))
        fenced_generation = plan.get("fence_generation")

    # All production adapters are idempotent at their own boundary. Reissuing a
    # claimed-but-unverified effect is the crash-recovery path; the durable
    # external-effect ledger prevents a verified effect from firing twice.
    try:
        result = adapter(plan)
        failure = _effect_failed(result)
        if failure:
            raise RuntimeError(failure)
        if _effect_pending(result):
            verified = external_effects.mark_external_effect_issued(
                str(ledger.get("effect_key") or ""),
                readback=_map(result),
                actor=actor,
                project=project,
            )
        else:
            verified = external_effects.verify_external_effect(
                str(ledger.get("effect_key") or ""),
                readback=_map(result),
                actor=actor,
                project=project,
            )
    except Exception as exc:
        external_effects.fail_external_effect(
            str(ledger.get("effect_key") or ""),
            str(exc),
            actor=actor,
            project=project,
        )
        raise
    return {
        "effect": effect,
        "route": plan.get("route"),
        "run": persisted,
        "plan": dict(plan),
        "result": result,
        "fenced_generation": fenced_generation,
        "receipt": {
            "schema": "switchboard.completion_effect_receipt.v1",
            "effect": effect,
            "idem_key": plan.get("idem_key"),
            "effect_key": ledger.get("effect_key"),
            "verified": (
                not _effect_pending(result)
                and not bool(verified.get("error"))
            ),
            "pending": _effect_pending(result),
            "idempotent_replay": not bool(ledger.get("claimed")),
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
    adapters: Optional[CompletionEffectAdapters] = None,
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
    if effect in {
        "ensure_review_generation", "start_remediation", "mark_ready",
        "enqueue", "requeue_merge_group", "repair_dispatch",
        "reconcile_provenance",
    }:
        return _execute_mutating_effect(
            effect,
            plan,
            decision=decision,
            snapshot=snapshot,
            project=project,
            actor=actor,
            adapters=adapters or CompletionEffectAdapters(),
            fence_generation=fence_generation,
        )
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
    wake_receipt = None
    if wake_completion_owner is not None:
        wake_receipt = wake_completion_owner(payload)
    return {
        "status": request.get("status") or "decision_recorded",
        "resumed": False,
        "wake": payload,
        "wake_receipt": wake_receipt,
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
    from switchboard.storage.repositories import attention as attention_store

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
        reason="completion_execution_receipt_recorded",
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
    "CompletionEffectAdapters",
    "execute_effect",
    "mark_human_resume_receipt",
    "resume_after_human_decision",
]
