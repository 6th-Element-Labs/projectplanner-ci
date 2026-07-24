"""Execute exactly one planned completion effect, then stop.

The planner is pure. This module is the side-effect boundary: persist the
completion run projection, perform the one effect, and return a receipt that
duplicate ticks can replay.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from switchboard.domain.completion.effects import canonical_findings, plan_effect
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
    fence_runner: Optional[EffectFn] = None
    reconcile_provenance: Optional[EffectFn] = None

    def for_effect(self, effect: str) -> Optional[EffectFn]:
        return getattr(self, effect, None)


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _completion_run_data(
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    decision, snapshot, plan = _map(decision), _map(snapshot), _map(plan)
    return {
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
                "effect": plan.get("effect"),
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
    }


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

    return completion_runs.transition_completion_run(
        _completion_run_data(decision, snapshot, plan),
        actor=actor,
        project=project,
    )


def ensure_completion_run(
    *,
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    current: Mapping[str, Any] | None,
    actor: str,
    project: str,
) -> dict[str, Any]:
    """Persist classified authority before deriving an effect identity.

    A first tick has no durable ``run_id``, ``state_version``, or ``attempt``.
    Persisting that authority first gives the initial effect and every replay
    the same identity. If authority already matches, preserve the current row
    so this bootstrap step cannot discard richer execution evidence.
    """
    decision, snapshot, current = (
        _map(decision), _map(snapshot), _map(current))
    expected = {
        "task_id": str(snapshot.get("task_id") or "").strip().upper(),
        "pr_number": int(snapshot.get("pr_number") or 0),
        "head_sha": str(snapshot.get("head_sha") or "").strip().lower(),
        "state": str(decision.get("state") or "blocked").strip().lower(),
        "route": str(decision.get("route") or "").strip().lower(),
        "reason_code": str(decision.get("reason_code") or "").strip(),
        "desired_role": str(decision.get("desired_role") or "").strip(),
        "board_status": str(
            decision.get("board_projection") or "In Review").strip(),
    }
    if current and all(
        (
            int(current.get(key) or 0) == value
            if key == "pr_number"
            else str(current.get(key) or "").strip().lower()
            == str(value or "").strip().lower()
        )
        for key, value in expected.items()
    ):
        return current
    return _persist_run(
        decision=decision,
        snapshot=snapshot,
        plan={},
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
    from switchboard.storage.repositories import attention as attention_store
    from switchboard.storage.repositories import completion_runs

    def write() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        with attention_store._conn(project) as c:
            persisted_row = completion_runs.transition_completion_run_in(
                c,
                _completion_run_data(decision, snapshot, plan),
                actor=actor,
            )
            # Prefer the durable run identity for the frozen closeout context.
            closeout_run = {
                **_map(run),
                "run_id": (
                    persisted_row.get("run_id") or _map(run).get("run_id")
                ),
                "state_version": (
                    persisted_row.get("state_version")
                    or _map(run).get("state_version")
                ),
                "attempt": (
                    persisted_row.get("attempt") or _map(run).get("attempt")
                ),
            }
            # Rebuild against the persisted identity before creating the
            # request. Both rows commit or roll back together.
            durable = plan_effect(decision, snapshot, closeout_run)
            request_data = build_human_closeout_request(
                plan=durable,
                decision=decision,
                snapshot=snapshot,
                run=closeout_run,
            )
            request = attention_store.create_attention_request_in(
                c, request_data, actor=actor, project=project,
            )
            return persisted_row, durable, request

    persisted, durable_plan, attention = attention_store._write_through(
        project, write)
    fenced_generation = None
    # Terminalize the live generation once when the human closeout is first
    # persisted. Replays must not re-fence Watch/session evidence.
    if (
        attention.get("created")
        and durable_plan.get("fence_required")
        and fence_generation is not None
    ):
        fence_generation(_map(durable_plan.get("fence_identity")))
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
            "verified": True,
            "pending": False,
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
        "fence_identity": _map(plan.get("fence_identity")),
        "acceptance_findings": canonical_findings(
            plan.get("acceptance_findings")),
        "escalated_findings": canonical_findings(
            plan.get("escalated_findings")),
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
                "verified": True,
                "pending": False,
                "idempotent_replay": True,
            },
        }
    existing_effect = _map(ledger.get("effect"))
    if not ledger.get("claimed") and existing_effect.get("status") == "issued":
        # The adapter crossed its external boundary. Reissuing before
        # authoritative readback could duplicate a runner, merge-queue
        # admission, or GitHub mutation.
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
                "reason": "effect_issued_awaiting_readback",
            },
        }
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
    if not ledger.get("claimed") and existing_effect.get("status") == "failed":
        retry_count = int(existing_effect.get("retry_count") or 0)
        retry_after = min(300.0, 5.0 * (2 ** min(max(retry_count - 1, 0), 6)))
        age = time.time() - float(
            existing_effect.get("updated_at") or time.time())
        if age < retry_after:
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
                    "reason": "effect_retry_backoff",
                    "retry_after_seconds": retry_after - age,
                },
            }
        ledger = external_effects.retry_external_effect(
            str(ledger.get("effect_key") or ""),
            expected_retry_count=retry_count,
            actor=actor,
            project=project,
        )
        if not ledger.get("claimed"):
            return {
                "effect": effect,
                "route": plan.get("route"),
                "run": persisted,
                "plan": dict(plan),
                "result": _map(ledger.get("effect")).get("readback") or {},
                "receipt": {
                    "schema": "switchboard.completion_effect_receipt.v1",
                    "effect": effect,
                    "idem_key": plan.get("idem_key"),
                    "effect_key": ledger.get("effect_key"),
                    "verified": False,
                    "pending": True,
                    "idempotent_replay": True,
                    "reason": "effect_retry_claim_lost",
                },
            }
    elif (
        not ledger.get("claimed")
        and existing_effect.get("status") in {"dead_letter", "void"}
    ):
        raise RuntimeError(
            f"completion effect is {existing_effect.get('status')}: "
            f"{existing_effect.get('last_error') or ledger.get('reason') or ''}"
        )

    fenced_generation = None
    if plan.get("fence_required") and fence_generation is not None:
        fence_generation(_map(plan.get("fence_identity")))
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
                "verified": True,
                "pending": False,
            },
        }
    if effect in {
        "ensure_review_generation", "start_remediation", "mark_ready",
        "enqueue", "requeue_merge_group", "repair_dispatch", "fence_runner",
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
    """Compatibility readback for the durable completion-wake outbox.

    Production wake issuance belongs to
    ``attention.attempt_completion_wake``.  Calling an arbitrary callback from
    this helper would bypass the transactional decision/outbox boundary.
    """
    del project, actor, wake_completion_owner
    decided = _map(decided)
    request = _map(decided.get("request"))
    wake = _map(decided.get("completion_wake"))
    return {
        "status": request.get("status") or "decision_recorded",
        "resumed": False,
        "wake": wake,
        "wake_receipt": wake.get("wake_receipt"),
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
    """Reject unbound receipts; only an exact fenced owner tick may resolve."""
    del request_id, expected_version, host_id, actor, receipt, project
    from switchboard.storage.repositories.attention import AttentionStoreError

    raise AttentionStoreError(
        "attention_completion_owner_required",
        "completion resume receipts require an exact fenced completion-owner tick",
    )


# Expose resume helpers as attributes for the test import style
# ``execute_effect.resume_after_human_decision``.
execute_effect.resume_after_human_decision = resume_after_human_decision  # type: ignore[attr-defined]
execute_effect.mark_human_resume_receipt = mark_human_resume_receipt  # type: ignore[attr-defined]


__all__ = [
    "CompletionEffectAdapters",
    "ensure_completion_run",
    "execute_effect",
    "mark_human_resume_receipt",
    "resume_after_human_decision",
]
