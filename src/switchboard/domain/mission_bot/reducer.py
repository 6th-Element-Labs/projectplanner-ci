"""Mission Bot reducer — ordered facts in, one of eight outputs out.

No classifier. No attribution. No invented human involvement.

```
merged               → OBSERVE_MERGED   (outranks stale human blockers)
agent_requires_human → AGENT_REQUIRES_HUMAN  (authenticated agent only)
live runner          → WAIT
unmet dependencies   → WAIT
no PR yet            → START_IMPLEMENTATION
factory failure      → START_REMEDIATION(full dossier)
draft                → MARK_READY
ci pending           → WAIT
review missing       → START_REVIEW
green                → ARM_MERGE
else                 → WAIT
```
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from switchboard.domain.mission_bot import facts
from switchboard.domain.mission_bot.dossier import build_dossier, evidence_identity
from switchboard.domain.mission_bot.outputs import (
    MISSION_BOT_VERSION,
    MISSION_COMMAND_SCHEMA,
    MissionOutput,
)


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _task_id(value: Any) -> str:
    """Preserve the canonical board identifier; it is not an enum value."""
    return str(value or "").strip().upper()


def _command(
    output: MissionOutput,
    *,
    snapshot: Mapping[str, Any],
    reason_code: str,
    role: str | None = None,
    dossier: Mapping[str, Any] | None = None,
    wait_reason: str = "",
) -> dict[str, Any]:
    snap = _map(snapshot)
    evidence = evidence_identity(dossier or {})
    payload = {
        "output": output.value,
        "task_id": _task_id(snap.get("task_id")),
        "pr_number": int(snap.get("pr_number") or 0),
        "head_sha": _text(snap.get("head_sha")).lower(),
        "reason_code": reason_code,
        "role": role or "",
        # Fresh CI/evidence identity must mint a new mission key.
        "evidence_identity": evidence,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    idem_key = f"mission:{payload['task_id'] or 'unknown'}:{digest[:32]}"
    command = {
        "schema": MISSION_COMMAND_SCHEMA,
        "mission_bot_version": MISSION_BOT_VERSION,
        "output": output.value,
        "reason_code": reason_code,
        "task_id": payload["task_id"],
        "pr_number": payload["pr_number"],
        "head_sha": payload["head_sha"],
        "role": role,
        "dossier": dict(dossier) if dossier else None,
        "wait_reason": wait_reason or None,
        "evidence_identity": evidence,
        "idempotency_key": idem_key,
        # Legacy completion/conformance surfaces still read idem_key.
        "idem_key": idem_key,
        # Compatibility projection for existing completion_run / UI surfaces.
        "route": _compat_route(output),
        "desired_role": role,
        "board_projection": _compat_board(output, snap),
        "state": _compat_state(output),
        "effect": _compat_effect(output),
    }
    return command


def _compat_route(output: MissionOutput) -> str:
    return {
        MissionOutput.START_IMPLEMENTATION: "implementation",
        MissionOutput.START_REMEDIATION: "remediation",
        MissionOutput.START_REVIEW: "review_merge",
        MissionOutput.MARK_READY: "review_merge",
        MissionOutput.ARM_MERGE: "review_merge",
        MissionOutput.WAIT: "wait",
        MissionOutput.AGENT_REQUIRES_HUMAN: "human",
        MissionOutput.OBSERVE_MERGED: "reconcile",
    }[output]


def _compat_board(output: MissionOutput, snap: Mapping[str, Any]) -> str:
    if output is MissionOutput.OBSERVE_MERGED:
        return "Done"
    if output is MissionOutput.AGENT_REQUIRES_HUMAN:
        return "Blocked"
    if output is MissionOutput.START_REMEDIATION:
        return "Blocked"
    if output is MissionOutput.START_IMPLEMENTATION:
        return "In Progress"
    return str(snap.get("board_status") or "In Review")


def _compat_state(output: MissionOutput) -> str:
    return {
        MissionOutput.START_IMPLEMENTATION: "implementing",
        MissionOutput.START_REMEDIATION: "blocked",
        MissionOutput.START_REVIEW: "assessing",
        MissionOutput.MARK_READY: "ready_to_queue",
        MissionOutput.ARM_MERGE: "ready_to_queue",
        MissionOutput.WAIT: "waiting",
        MissionOutput.AGENT_REQUIRES_HUMAN: "blocked",
        MissionOutput.OBSERVE_MERGED: "reconciling",
    }[output]


def _compat_effect(output: MissionOutput) -> str:
    return {
        MissionOutput.START_IMPLEMENTATION: "start_implementation",
        MissionOutput.START_REMEDIATION: "start_remediation",
        MissionOutput.START_REVIEW: "ensure_review_generation",
        MissionOutput.MARK_READY: "mark_ready",
        MissionOutput.ARM_MERGE: "enqueue",
        MissionOutput.WAIT: "wait",
        MissionOutput.AGENT_REQUIRES_HUMAN: "agent_requires_human",
        MissionOutput.OBSERVE_MERGED: "reconcile_provenance",
    }[output]


def reduce_mission(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce one exact-head fact snapshot to exactly one Mission Bot command."""
    snap = _map(snapshot)

    # 1. Canonical merge observation owns Done — outranks a stale human blocker.
    if facts.is_merged(snap):
        return _command(
            MissionOutput.OBSERVE_MERGED,
            snapshot=snap,
            reason_code="canonical_pr_merged",
        )

    # 2. Only an authenticated agent receipt may require a human.
    if facts.agent_requires_human(snap):
        blocker = _map(_map(_map(snap.get("work_session")).get("hygiene")).get("blocker"))
        if not blocker:
            blocker = _map(
                _map(snap.get("task")).get("human_blocker")
                or _map(snap.get("task")).get("agent_requires_human")
                or snap.get("agent_requires_human")
                or snap.get("attention")
            )
        reason = _text(blocker.get("reason")) or "agent_requires_human"
        dossier = build_dossier(
            snap, reason_code=reason, mission="surface_agent_requires_human",
        )
        return _command(
            MissionOutput.AGENT_REQUIRES_HUMAN,
            snapshot=snap,
            reason_code=reason,
            dossier=dossier,
        )

    # 3. Hang up while a live runner owns capacity.
    if facts.live_runner(snap):
        runner = _map(snap.get("runner"))
        return _command(
            MissionOutput.WAIT,
            snapshot=snap,
            reason_code="live_runner_in_progress",
            wait_reason=_text(runner.get("role")) or "live_runner",
        )

    # 4. Unmet dependencies — wait; do not emit a doomed start_task.
    if facts.dependencies_unsatisfied(snap):
        dep = facts.dependency_state(snap)
        dossier = build_dossier(
            snap, reason_code="unmet_dependencies", mission="wait_dependencies",
        )
        return _command(
            MissionOutput.WAIT,
            snapshot=snap,
            reason_code="unmet_dependencies",
            wait_reason="dependencies",
            dossier=dossier,
        )

    # 5. No PR yet — boot the implementer.
    if facts.needs_implementation(snap):
        dossier = build_dossier(
            snap, reason_code="needs_implementation", mission="implement",
        )
        return _command(
            MissionOutput.START_IMPLEMENTATION,
            snapshot=snap,
            reason_code="needs_implementation",
            role="implementation",
            dossier=dossier,
        )

    # 6. Factory failure — boot remediation with the full unchanged dossier.
    #    GitHub/Switchboard red never becomes human. Pending CI is not a failure.
    if facts.factory_failure(snap):
        reason = facts.factory_failure_reason(snap)
        dossier = build_dossier(
            snap, reason_code=reason, mission="remediate",
        )
        return _command(
            MissionOutput.START_REMEDIATION,
            snapshot=snap,
            reason_code=reason,
            role="remediation",
            dossier=dossier,
        )

    # 7. Draft PR — mark ready (free mechanical step; outranks pending CI).
    if facts.is_draft(snap):
        return _command(
            MissionOutput.MARK_READY,
            snapshot=snap,
            reason_code="draft_ready_to_mark_ready",
            role="review_merge",
        )

    # 8. CI still pending/hydrating — wait.
    if facts.ci_pending(snap):
        return _command(
            MissionOutput.WAIT,
            snapshot=snap,
            reason_code="required_exact_head_ci_pending",
            wait_reason="ci_pending",
        )

    # 9. Merge queue in flight — wait.
    if facts.queue_waiting(snap):
        queue = _map(snap.get("merge_queue"))
        state = _text(queue.get("state") or queue.get("status")) or "queued"
        return _command(
            MissionOutput.WAIT,
            snapshot=snap,
            reason_code=f"merge_queue_{state}",
            wait_reason="merge_queue",
        )

    # 10. Review missing/stale — boot review.
    if facts.review_needed(snap):
        reason = (
            "review_verdict_stale"
            if _text(_map(snap.get("review")).get("status")
                     or _map(snap.get("review")).get("state")
                     or _map(snap.get("review")).get("verdict"))
            in {"pass", "passed", "approved", "success"}
            else "review_required"
        )
        dossier = build_dossier(snap, reason_code=reason, mission="review")
        return _command(
            MissionOutput.START_REVIEW,
            snapshot=snap,
            reason_code=reason,
            role="review_merge",
            dossier=dossier,
        )

    # 11. Green gates — arm merge.
    if facts.gates_green(snap):
        return _command(
            MissionOutput.ARM_MERGE,
            snapshot=snap,
            reason_code="exact_head_gates_passed",
            role="review_merge",
        )

    # 12. Nothing actionable.
    return _command(
        MissionOutput.WAIT,
        snapshot=snap,
        reason_code="no_actionable_mission",
        wait_reason="idle",
    )


__all__ = ["reduce_mission"]
