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


def _human_action(reason: str) -> dict[str, Any]:
    """Return truthful operator copy and choices for one human reason."""
    normalized = reason.strip().lower()
    if any(token in normalized for token in (
        "credential", "permission", "authority", "policy", "approval",
    )):
        return {
            "why": (
                "An eligible credential, authenticated host, approval, or policy "
                "authority required for the remaining proof is unavailable."
            ),
            "prompt": "Supply the missing authority or keep the task blocked.",
            "choices": [
                {"id": "supply_credential",
                 "label": "Supply eligible authenticated host or credential",
                 "effect": "resume_assessment"},
                {"id": "authorize_policy",
                 "label": "Record an authorized policy decision",
                 "effect": "resume_assessment"},
                {"id": "hold", "label": "Keep blocked",
                 "effect": "remain_blocked"},
            ],
        }
    if normalized in {"wrong_target_branch", "pr_branch_behind"}:
        return {
            "why": "The pull request targets or tracks the wrong branch for canonical landing.",
            "prompt": "Correct the pull request branch authority or keep the task blocked.",
            "choices": [
                {"id": "correct_target_branch",
                 "label": "Correct the target or branch",
                 "effect": "resume_assessment"},
                {"id": "authorize_current_target",
                 "label": "Authorize the current target",
                 "effect": "resume_assessment"},
                {"id": "hold", "label": "Keep blocked",
                 "effect": "remain_blocked"},
            ],
        }
    if normalized in {"canonical_repo_missing", "repo_role_cannot_merge"}:
        return {
            "why": "Canonical repository or merge authority is not configured for this task.",
            "prompt": "Configure canonical repository authority or keep the task blocked.",
            "choices": [
                {"id": "configure_canonical_repo",
                 "label": "Configure canonical repository authority",
                 "effect": "resume_assessment"},
                {"id": "assign_merge_authority",
                 "label": "Assign an eligible merge authority",
                 "effect": "resume_assessment"},
                {"id": "hold", "label": "Keep blocked",
                 "effect": "remain_blocked"},
            ],
        }
    if normalized == "pr_closed_unmerged":
        return {
            "why": "The pull request closed without canonical merge provenance.",
            "prompt": "Reopen or replace the pull request, or keep the task blocked.",
            "choices": [
                {"id": "reopen_pull_request", "label": "Reopen the pull request",
                 "effect": "resume_assessment"},
                {"id": "create_replacement_pull_request",
                 "label": "Create a replacement pull request",
                 "effect": "resume_assessment"},
                {"id": "hold", "label": "Keep blocked",
                 "effect": "remain_blocked"},
            ],
        }
    if "retry" in normalized or normalized in {
        "human_review_findings", "unclassified_failed_gate",
    }:
        return {
            "why": (
                "Automation exhausted its safe retry or encountered a judgment "
                "finding that requires an operator decision."
            ),
            "prompt": "Resolve the named finding, extend the retry, or keep the task blocked.",
            "choices": [
                {"id": "resolve_finding", "label": "Resolve the named finding",
                 "effect": "resume_assessment"},
                {"id": "extend_retry_budget", "label": "Authorize another retry",
                 "effect": "resume_assessment"},
                {"id": "hold", "label": "Keep blocked",
                 "effect": "remain_blocked"},
            ],
        }
    return {
        "why": f"Automation cannot safely resolve the remaining gate ({reason}).",
        "prompt": "Resolve the named blocker or keep the task blocked.",
        "choices": [
            {"id": "resolve_blocker", "label": "Resolve the named blocker",
             "effect": "resume_assessment"},
            {"id": "hold", "label": "Keep blocked",
             "effect": "remain_blocked"},
        ],
    }


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
    action = _human_action(reason)
    choices = action["choices"]
    recommended = choices[0]
    review = _map(snapshot.get("review"))
    contexts = _map(snapshot.get("status_contexts"))
    passed_review = str(
        review.get("status") or review.get("state") or review.get("verdict") or ""
    ).strip().lower() in {"pass", "passed", "approved", "success"}
    observed_ci = bool(contexts)
    evidence_summary = "Implementation reached"
    if passed_review and observed_ci:
        evidence_summary += " exact-head review and CI assessment at"
    elif passed_review:
        evidence_summary += " exact-head review assessment at"
    elif observed_ci:
        evidence_summary += " exact-head CI assessment at"
    else:
        evidence_summary += " completion assessment at"
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
            f"{evidence_summary} PR #{pr_number} / {head_sha[:12]}; "
            f"the remaining gate is {reason}."
        ),
        "evidence_refs": evidence,
        "unresolved_gate": reason,
        "reason_code": reason,
        "why_automation_stopped": action["why"],
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
            f"{context['task_id']} needs you: {reason}. {action['prompt']} "
            "Autopilot will re-assess the exact current head after the decision."
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
