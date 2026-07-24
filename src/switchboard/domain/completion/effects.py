"""Plan exactly one idempotent effect for a classified completion decision.

The classifier says *what is true*; this module says *what to do about it once*.
Keeping it pure means the rules that actually cause outages -- a live
``review_merge`` winning over newer evidence that demands remediation, a
duplicate tick enqueueing twice, a lease heartbeat inventing a new effect --
are provable without GitHub, a board, or a runner.

The executor's contract is: plan one effect, perform it, then rehydrate and
classify again.  Never two effects in one tick.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


EFFECT_SCHEMA = "switchboard.completion_effect.v1"

#: Effects that change something outside the completion run itself.
MUTATING_EFFECTS = frozenset({
    "ensure_review_generation", "start_remediation", "mark_ready", "enqueue",
    "requeue_merge_group", "repair_dispatch", "fence_runner", "escalate_human",
    "reconcile_provenance",
})

#: Effects that must happen at most once per completion decision.
ONCE_ONLY_EFFECTS = frozenset({"enqueue", "escalate_human"})


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def canonical_findings(value: Any) -> list[dict[str, Any]]:
    """Return a deterministic finding set for effect identity."""
    rows = [
        _map(item) for item in (value or [])
        if isinstance(item, Mapping)
    ]
    return sorted(
        rows,
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), default=str),
    )


def _effect_for(route: str, decision_effect: str,
                snapshot: Mapping[str, Any]) -> str:
    if decision_effect == "fence_runner":
        return "fence_runner"
    if route == "none":
        return "none"
    if route == "wait":
        return "wait"
    if route == "reconcile":
        return "reconcile_provenance"
    if route == "human":
        return "escalate_human"
    if route == "remediation":
        return "start_remediation"
    if route == "review_merge":
        if decision_effect == "mark_ready_then_reread":
            return "mark_ready"
        if decision_effect == "enqueue":
            return "enqueue"
        return "ensure_review_generation"
    if route == "coordination_retry":
        queue = _map(snapshot.get("merge_queue"))
        if _text(queue.get("state") or queue.get("status")) == "unmergeable":
            # An infrastructure-failed merge group is requeued, not rebuilt.
            return "requeue_merge_group"
        return "repair_dispatch"
    return "repair_dispatch"


def _runner_fence_identity(runner: Mapping[str, Any]) -> dict[str, Any]:
    identity = _map(runner.get("execution"))
    metadata = _map(runner.get("metadata"))
    return {
        "runner_session_id": str(runner.get("runner_session_id") or ""),
        "execution_id": str(
            runner.get("execution_id") or identity.get("execution_id") or ""),
        "execution_connection_id": str(
            runner.get("execution_connection_id")
            or metadata.get("execution_connection_id") or ""),
        "generation": (
            runner.get("generation")
            or runner.get("execution_generation")
            or identity.get("generation")
        ),
        "fence_epoch": (
            runner.get("fence_epoch")
            or runner.get("lease_epoch")
            or identity.get("fence_epoch")
        ),
        "role": str(
            runner.get("role")
            or runner.get("execution_role")
            or identity.get("role") or ""),
        "head_sha": str(
            runner.get("head_sha")
            or runner.get("execution_head_sha")
            or identity.get("head_sha") or ""),
    }


def _fence(snapshot: Mapping[str, Any], desired_role: str,
           head_sha: str) -> tuple[bool, dict[str, Any]]:
    """A live generation may be kept only if role AND exact head both match."""
    runner = _map(snapshot.get("runner"))
    if not runner or not runner.get("live"):
        return False, {}
    runner_role = _text(runner.get("role") or runner.get("execution_role"))
    runner_head = str(runner.get("head_sha") or "").strip()
    if desired_role and runner_role == _text(desired_role) and runner_head == head_sha:
        return False, {}
    return True, _runner_fence_identity(runner)


def effect_key(run: Mapping[str, Any], snapshot: Mapping[str, Any],
               route: str, desired_role: str, *,
               acceptance_findings: Any = None,
               escalated_findings: Any = None,
               fence_identity: Any = None) -> str:
    """Stable for one repair contract; changes with findings or execution identity.

    Deliberately excludes every continuously changing liveness value (lease
    renewals, heartbeats, ``expires_at``), which would otherwise make each tick
    look like new work.
    """
    payload = {
        "run_id": str(run.get("run_id") or ""),
        "state_version": int(run.get("state_version") or 0),
        "task_id": str(snapshot.get("task_id") or "").strip().upper(),
        "pr_number": snapshot.get("pr_number"),
        "head_sha": str(snapshot.get("head_sha") or "").strip().lower(),
        "route": _text(route),
        "desired_role": _text(desired_role),
        "attempt": int(run.get("attempt") or 0),
        "acceptance_findings": canonical_findings(acceptance_findings),
        "escalated_findings": canonical_findings(escalated_findings),
        # Immutable execution identity is part of a stop/replace decision.
        # Heartbeats and expiry are intentionally absent.
        "fence_identity": _map(fence_identity),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return f"completion:{payload['task_id'] or 'unknown'}:{digest[:32]}"


def plan_effect(decision: Mapping[str, Any], snapshot: Mapping[str, Any],
                run: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the single effect to perform for this decision."""
    decision = _map(decision)
    snapshot = _map(snapshot)
    run = _map(run)
    route = _text(decision.get("route"))
    desired_role = decision.get("desired_role") or ""
    head_sha = str(snapshot.get("head_sha") or "").strip()
    acceptance_findings = canonical_findings(
        decision.get("acceptance_findings"))
    escalated_findings = canonical_findings(
        decision.get("escalated_findings"))

    effect = _effect_for(route, _text(decision.get("effect")), snapshot)
    fence_required, fence_identity = _fence(
        snapshot, desired_role, head_sha)

    # Precedence: the classifier decides before any running process does. A
    # live generation is attached to only when it already matches the desired
    # role at the exact head; otherwise it is fenced and replaced.
    if effect in {"ensure_review_generation", "start_remediation"} and not fence_required:
        runner = _map(snapshot.get("runner"))
        if runner.get("live"):
            effect = "attach_and_wait"

    return {
        "schema": EFFECT_SCHEMA,
        "effect": effect,
        "route": route,
        "role": desired_role or None,
        "reason_code": decision.get("reason_code"),
        "task_id": str(snapshot.get("task_id") or "").strip().upper(),
        "pr_number": snapshot.get("pr_number"),
        "head_sha": head_sha,
        "completion_run_id": str(run.get("run_id") or ""),
        "decision_attempt": int(run.get("attempt") or 0),
        "state_version": int(run.get("state_version") or 0),
        "board_projection": decision.get("board_projection"),
        "acceptance_findings": acceptance_findings,
        "escalated_findings": escalated_findings,
        "fence_required": fence_required,
        "fence_generation": (
            fence_identity.get("generation") if fence_required else None
        ),
        "fence_identity": fence_identity if fence_required else None,
        "queue_remediation_round": effect == "start_remediation",
        "reread_after": effect == "mark_ready",
        "once_only": effect in ONCE_ONLY_EFFECTS,
        "mutates": effect in MUTATING_EFFECTS,
        "idem_key": effect_key(
            run,
            snapshot,
            route,
            desired_role,
            acceptance_findings=acceptance_findings,
            escalated_findings=escalated_findings,
            fence_identity=fence_identity if fence_required else None,
        ),
    }


__all__ = [
    "EFFECT_SCHEMA",
    "MUTATING_EFFECTS",
    "ONCE_ONLY_EFFECTS",
    "canonical_findings",
    "effect_key",
    "plan_effect",
]
