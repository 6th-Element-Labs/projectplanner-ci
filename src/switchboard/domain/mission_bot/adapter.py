"""Execute one Mission Bot command through existing authoritative ports.

This adapter does not reinterpret policy. It maps each of the eight outputs to
exactly one port call:

- START_* → ``start_task``
- MARK_READY → GitHub mark-ready
- ARM_MERGE → GitHub auto-merge arm
- WAIT → persist wait observation
- AGENT_REQUIRES_HUMAN → persist sticky blocked (agent already recorded it)
- OBSERVE_MERGED → observe merge provenance

Mutating ports reuse the external-effect ledger for verified idempotent replay.
Failed port results must fail the ledger — never verify — so retries are not
permanently suppressed. In-flight / pending results stay pending — never
verified — so uncertain runner boots cannot wedge a mission.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from switchboard.domain.mission_bot.outputs import MissionOutput


EffectFn = Callable[[Mapping[str, Any]], Any]
PersistFn = Callable[..., Any]

_MUTATING_EFFECTS = frozenset({
    "start_implementation",
    "start_remediation",
    "ensure_review_generation",
    "mark_ready",
    "enqueue",
})

_PENDING_ACTIONS = frozenset({
    "transitioning", "pending", "stopping", "starting", "awaiting_readback",
})


@dataclass(frozen=True)
class MissionPorts:
    """Authoritative ports the Mission Bot may call. No extras."""

    start_task: EffectFn
    mark_ready: EffectFn
    arm_merge: EffectFn
    persist_wait: PersistFn
    persist_agent_requires_human: PersistFn
    observe_merged: PersistFn


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _effect_failed(result: Any) -> str:
    """Mirror completion executor failure detection for Mission Bot receipts."""
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
    # Nested reconcile / start_task surfaces sometimes wrap failures.
    nested = _map(row.get("reconcile") or row.get("result"))
    if nested.get("error"):
        return str(nested.get("error") or "nested effect error")
    return ""


def _effect_pending(result: Any) -> bool:
    row = _map(result)
    return str(row.get("action") or row.get("status") or "").strip().lower() in (
        _PENDING_ACTIONS
    )


def _start_plan(command: Mapping[str, Any]) -> dict[str, Any]:
    dossier = _map(command.get("dossier"))
    return {
        "task_id": command.get("task_id"),
        "role": command.get("role"),
        "head_sha": command.get("head_sha") or dossier.get("head_sha") or "",
        "reason_code": command.get("reason_code"),
        "route": command.get("route"),
        "pr_number": command.get("pr_number") or dossier.get("pr_number") or 0,
        # Full dossier evidence — never strip CI URL/summary.
        "acceptance_findings": list(dossier.get("acceptance_findings") or []),
        "failing_contexts": list(dossier.get("failing_contexts") or []),
        "failing_check_url": dossier.get("failing_check_url") or "",
        "failing_run_attempt": int(dossier.get("failing_run_attempt") or 0),
        "failing_check_summary": dossier.get("failing_check_summary") or "",
        "dossier": dossier,
        "evidence_identity": _map(command.get("evidence_identity")),
        "idem_key": command.get("idem_key") or command.get("idempotency_key"),
        "decision_attempt": int(command.get("decision_attempt") or 0),
        "state_version": int(command.get("state_version") or 0),
    }


def _ledger_replay(
    *,
    effect: str,
    plan: Mapping[str, Any],
    project: str,
    actor: str,
) -> dict[str, Any] | None:
    """Return a replay decision when the ledger already owns this effect."""
    if effect not in _MUTATING_EFFECTS:
        return None
    from switchboard.storage.repositories import external_effects

    idem_key = str(plan.get("idem_key") or plan.get("idempotency_key") or "")
    if not idem_key:
        return None
    ledger = external_effects.claim_external_effect(
        "completion_effect",
        str(plan.get("task_id") or ""),
        effect,
        {
            # The assignment carries the complete dossier.  Ledger identity
            # must contain only the already-normalized mission identity;
            # otherwise elapsed clocks inside that dossier create a new
            # external mutation on every tick.
            "idem_key": idem_key,
            "effect": effect,
            "task_id": str(plan.get("task_id") or ""),
            "head_sha": str(plan.get("head_sha") or ""),
            "role": str(plan.get("role") or ""),
            "reason_code": str(plan.get("reason_code") or ""),
            "evidence_identity": _map(plan.get("evidence_identity")),
        },
        task_id=str(plan.get("task_id") or ""),
        idem_key=idem_key,
        actor=actor,
        project=project,
    )
    if ledger.get("verified"):
        return {
            "claimed": False,
            "ledger": ledger,
            "result": _map(ledger.get("proof") or ledger.get("readback")),
            "idempotent_replay": True,
            "verified": True,
            "pending": False,
        }
    if not ledger.get("claimed"):
        existing = _map(ledger.get("effect"))
        if str(existing.get("status") or "").lower() == "failed":
            # Mission Bot has no human/factory-failure classifier. A failed
            # boot is retried by a fresh tick through one ledger CAS; start_task
            # and the execution lease remain the duplicate-run authorities.
            retried = external_effects.retry_external_effect(
                str(ledger.get("effect_key") or ""),
                expected_retry_count=int(existing.get("retry_count") or 0),
                actor=actor,
                project=project,
            )
            if retried.get("claimed"):
                return {
                    "claimed": True,
                    "ledger": retried,
                    # This tick owns a fresh issuance attempt; do not take the
                    # readback-only replay branch in _run_mutating.
                    "idempotent_replay": False,
                    "retry": True,
                    "verified": False,
                    "pending": False,
                }
        # Issued/in-flight — do not double-fire, and do not mark verified.
        return {
            "claimed": False,
            "ledger": ledger,
            "result": {"action": "awaiting_readback"},
            "idempotent_replay": True,
            "verified": False,
            "pending": True,
        }
    return {
        "claimed": True,
        "ledger": ledger,
        "idempotent_replay": False,
        "verified": False,
        "pending": False,
    }


def _ledger_settle(
    ledger_claim: Mapping[str, Any] | None,
    *,
    result: Mapping[str, Any],
    project: str,
    actor: str,
) -> dict[str, bool]:
    """Verify on success; issue on pending; fail on error."""
    failure = _effect_failed(result)
    pending = _effect_pending(result)
    if not ledger_claim or not ledger_claim.get("claimed"):
        return {
            "verified": (not failure) and (not pending),
            "pending": pending and not failure,
        }
    from switchboard.storage.repositories import external_effects

    effect_key = str(
        _map(ledger_claim.get("ledger")).get("effect_key")
        or ledger_claim.get("effect_key")
        or ""
    )
    if not effect_key:
        return {
            "verified": (not failure) and (not pending),
            "pending": pending and not failure,
        }
    if failure:
        external_effects.fail_external_effect(
            effect_key,
            failure,
            readback=dict(result),
            actor=actor,
            project=project,
        )
        return {"verified": False, "pending": False}
    if pending:
        external_effects.mark_external_effect_issued(
            effect_key,
            readback=dict(result),
            actor=actor,
            project=project,
        )
        return {"verified": False, "pending": True}
    external_effects.verify_external_effect(
        effect_key,
        readback=dict(result),
        actor=actor,
        project=project,
    )
    return {"verified": True, "pending": False}


def _run_mutating(
    *,
    ports_fn: EffectFn,
    plan: Mapping[str, Any],
    effect: str,
    project: str,
    actor: str,
) -> tuple[Any, dict[str, Any] | None, bool, bool]:
    ledger_claim = _ledger_replay(
        effect=effect, plan=plan, project=project, actor=actor,
    )
    if ledger_claim and ledger_claim.get("idempotent_replay"):
        result = ledger_claim["result"]
        return (
            result,
            ledger_claim,
            bool(ledger_claim.get("verified")) and not bool(_effect_failed(result)),
            bool(ledger_claim.get("pending")),
        )
    try:
        result = ports_fn(plan)
    except Exception as exc:
        # Claiming the ledger and calling the port are one guarded issuance
        # boundary.  A raised start_task/GitHub exception must never leave a
        # permanent "claimed" receipt that looks in-flight forever.
        if ledger_claim and ledger_claim.get("claimed"):
            from switchboard.storage.repositories import external_effects

            effect_key = str(
                _map(ledger_claim.get("ledger")).get("effect_key")
                or ledger_claim.get("effect_key")
                or ""
            )
            if effect_key:
                external_effects.fail_external_effect(
                    effect_key,
                    f"{type(exc).__name__}: {exc}",
                    readback={
                        "error": str(exc),
                        "exception_type": type(exc).__name__,
                    },
                    actor=actor,
                    project=project,
                )
        raise
    settled = _ledger_settle(
        ledger_claim, result=_map(result), project=project, actor=actor,
    )
    return result, ledger_claim, settled["verified"], settled["pending"]


def execute_mission_command(
    command: Mapping[str, Any],
    *,
    ports: MissionPorts,
    decision: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    run: Mapping[str, Any] | None = None,
    project: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Execute exactly one Mission Bot command. No policy overlay."""
    del decision  # command is sole authority
    cmd = _map(command)
    try:
        output = MissionOutput(str(cmd.get("output") or ""))
    except ValueError as exc:
        raise ValueError(f"unsupported mission output: {cmd.get('output')!r}") from exc

    ledger_claim: dict[str, Any] | None = None
    verified = True
    pending = False
    if output is MissionOutput.START_IMPLEMENTATION:
        plan = _start_plan({**cmd, "role": "implementation"})
        effect = "start_implementation"
        result, ledger_claim, verified, pending = _run_mutating(
            ports_fn=ports.start_task, plan=plan, effect=effect,
            project=project, actor=actor,
        )
    elif output is MissionOutput.START_REMEDIATION:
        plan = _start_plan({**cmd, "role": "remediation"})
        effect = "start_remediation"
        result, ledger_claim, verified, pending = _run_mutating(
            ports_fn=ports.start_task, plan=plan, effect=effect,
            project=project, actor=actor,
        )
    elif output is MissionOutput.START_REVIEW:
        plan = _start_plan({**cmd, "role": "review_merge"})
        effect = "ensure_review_generation"
        result, ledger_claim, verified, pending = _run_mutating(
            ports_fn=ports.start_task, plan=plan, effect=effect,
            project=project, actor=actor,
        )
    elif output is MissionOutput.MARK_READY:
        plan = {
            "pr_number": cmd.get("pr_number"),
            "head_sha": cmd.get("head_sha"),
            "task_id": cmd.get("task_id"),
            "idem_key": cmd.get("idem_key") or cmd.get("idempotency_key"),
        }
        effect = "mark_ready"
        result, ledger_claim, verified, pending = _run_mutating(
            ports_fn=ports.mark_ready, plan=plan, effect=effect,
            project=project, actor=actor,
        )
    elif output is MissionOutput.ARM_MERGE:
        plan = {
            "pr_number": cmd.get("pr_number"),
            "head_sha": cmd.get("head_sha"),
            "task_id": cmd.get("task_id"),
            "idem_key": cmd.get("idem_key") or cmd.get("idempotency_key"),
        }
        effect = "enqueue"
        result, ledger_claim, verified, pending = _run_mutating(
            ports_fn=ports.arm_merge, plan=plan, effect=effect,
            project=project, actor=actor,
        )
    elif output is MissionOutput.WAIT:
        result = ports.persist_wait(
            command=cmd, snapshot=snapshot or {}, run=run or {},
            project=project, actor=actor,
        )
        effect = "wait"
        verified = not bool(_effect_failed(result)) and not _effect_pending(result)
        pending = _effect_pending(result) and not bool(_effect_failed(result))
    elif output is MissionOutput.AGENT_REQUIRES_HUMAN:
        result = ports.persist_agent_requires_human(
            command=cmd, snapshot=snapshot or {}, run=run or {},
            project=project, actor=actor,
        )
        effect = "agent_requires_human"
        verified = not bool(_effect_failed(result)) and not _effect_pending(result)
        pending = _effect_pending(result) and not bool(_effect_failed(result))
    elif output is MissionOutput.OBSERVE_MERGED:
        result = ports.observe_merged(
            command=cmd, snapshot=snapshot or {}, run=run or {},
            project=project, actor=actor,
        )
        effect = "reconcile_provenance"
        verified = not bool(_effect_failed(result)) and not _effect_pending(result)
        pending = _effect_pending(result) and not bool(_effect_failed(result))
    else:  # pragma: no cover - enum exhaustiveness
        raise ValueError(f"unhandled mission output: {output}")

    idempotent_replay = bool(
        ledger_claim and ledger_claim.get("idempotent_replay")
    )
    failure = _effect_failed(result)
    if failure:
        verified = False
        pending = False
    return {
        "effect": effect,
        "output": output.value,
        "route": cmd.get("route"),
        "run": _map(run),
        "plan": cmd,
        "command": cmd,
        "result": result,
        "receipt": {
            "schema": "switchboard.mission_effect_receipt.v1",
            "effect": effect,
            "output": output.value,
            "idem_key": cmd.get("idem_key") or cmd.get("idempotency_key"),
            "verified": bool(verified) and not bool(failure) and not bool(pending),
            "pending": bool(pending) and not bool(failure),
            "idempotent_replay": idempotent_replay,
            "error": failure or None,
        },
    }


__all__ = ["MissionPorts", "execute_mission_command"]
