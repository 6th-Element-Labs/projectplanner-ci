"""Frozen closeout payload for route=human completion decisions.

The attention_request is the authority. PR comments, CLI prose, and
agent_messages may mirror this payload but must never invent it.
"""
from __future__ import annotations

from typing import Any, Mapping


CLOSEOUT_SCHEMA = "switchboard.completion_human_closeout.v1"
PROVIDER = "switchboard.completion"


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_human_closeout_request(
    *,
    plan: Mapping[str, Any],
    decision: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    run: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one immutable PROTO-7 attention request for a human route."""
    plan, decision, snapshot, run = (
        _map(plan), _map(decision), _map(snapshot), _map(run),
    )
    task = _map(snapshot.get("task"))
    deliverable = _map(task.get("deliverable") or snapshot.get("deliverable"))
    reason = _text(decision.get("reason_code") or plan.get("reason_code"))
    head_sha = _text(plan.get("head_sha") or snapshot.get("head_sha"))
    pr_number = plan.get("pr_number") or snapshot.get("pr_number")
    evidence = {
        "ci": _map(snapshot.get("status_contexts")),
        "review": _map(snapshot.get("review")),
        "merge_gate": _map(snapshot.get("merge_gate")),
        "work_session": {
            "work_session_id": _map(snapshot.get("work_session")).get("work_session_id"),
        },
        "runner": {
            "generation": _map(snapshot.get("runner")).get("generation"),
            "role": _map(snapshot.get("runner")).get("role"),
            "head_sha": _map(snapshot.get("runner")).get("head_sha"),
            "live": bool(_map(snapshot.get("runner")).get("live")),
        },
        "completion_run": {
            "run_id": run.get("run_id"),
            "state_version": run.get("state_version"),
            "attempt": run.get("attempt"),
            "route": "human",
        },
    }
    choices = [
        {
            "id": "supply_credential",
            "label": "Supply eligible authenticated host or credential",
            "effect": "resume_assessment",
        },
        {
            "id": "authorize_policy",
            "label": "Record authorized policy exception",
            "effect": "resume_assessment",
        },
        {
            "id": "hold",
            "label": "Keep blocked — do not dispatch another coder",
            "effect": "remain_blocked",
        },
    ]
    recommended = choices[0]
    context = {
        "schema": CLOSEOUT_SCHEMA,
        "task_id": _text(plan.get("task_id") or snapshot.get("task_id")).upper(),
        "deliverable_id": _text(
            deliverable.get("deliverable_id") or deliverable.get("id")),
        "milestone_id": _text(deliverable.get("milestone_id")),
        "completion_run_id": _text(run.get("run_id")),
        "state_version": int(run.get("state_version") or 0),
        "pr_number": pr_number,
        "head_sha": head_sha,
        "completed_work_summary": (
            f"Implementation reached PR #{pr_number} at {head_sha[:12]} "
            "with exact-head review/CI evidence; automation cannot prove the "
            f"remaining gate ({reason})."
        ),
        "evidence_refs": evidence,
        "unresolved_gate": reason,
        "reason_code": reason,
        "why_automation_stopped": (
            "A credential, host, or policy authority required for live proof is "
            "absent. Dispatching another coder would not resolve the gate."
        ),
        "delivery_impact": (
            "Autopilot stays sticky Blocked(route=human) until an authorized "
            "decision supplies the missing authority."
        ),
        "owner": _text(
            task.get("owner_person_or_role")
            or task.get("assignee")
            or "operator"),
        "resume_condition": (
            "Authorized human decision recorded on this attention_request, then "
            "a provider/execution delivery receipt exists for the wake."
        ),
        "next_automatic_action": (
            "Wake the completion owner to rehydrate and classify the exact "
            "current head; do not start a new coding generation from this decision."
        ),
        "authority": "attention_request",
        "mirrors_only": ["pr_comment", "cli_closeout", "agent_message"],
    }
    idem_key = _text(plan.get("idem_key"))
    return {
        "provider": PROVIDER,
        "provider_request_id": f"completion-human:{idem_key}",
        "schema_version": CLOSEOUT_SCHEMA,
        "prompt": (
            f"{context['task_id']} needs you: {reason}. "
            "Supply an eligible authenticated host/credential or authorize the "
            "policy action so Autopilot can resume assessment."
        ),
        "choices": choices,
        "recommended_default": recommended,
        "idempotency_key": idem_key,
        "task_id": context["task_id"],
        "host_id": _text(_map(snapshot.get("runner")).get("host_id")) or "operator",
        "runner_session_id": _map(snapshot.get("runner")).get("runner_session_id"),
        "work_session_id": _map(snapshot.get("work_session")).get("work_session_id"),
        "context": context,
        "expires_at": plan.get("expires_at"),
    }


__all__ = [
    "CLOSEOUT_SCHEMA",
    "PROVIDER",
    "build_human_closeout_request",
]
