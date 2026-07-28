"""Ordered, read-only facts for the Mission Bot.

These helpers observe GitHub/Switchboard/agent-authored state. They never
decide remediation strategy or invent human involvement.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


_PASS = {"success", "passed", "pass", "ok", "neutral", "skipped"}
_PENDING = {"requested", "queued", "in_progress", "waiting", "pending", "expected"}
_FAILED = {
    "failure", "failed", "error", "timed_out", "action_required",
    "cancelled", "canceled", "stale", "startup_failure",
}
_QUEUE_WAIT = {"queued", "awaiting_checks", "mergeable", "locked"}
_AGENT_HUMAN_ROUTES = frozenset({"human", "agent_requires_human"})


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _text_raw(value: Any) -> str:
    return str(value or "").strip()


def agent_requires_human(snapshot: Mapping[str, Any]) -> bool:
    """True only for an agent-authored sticky blocker receipt.

    Machine classifiers must never create this fact. It comes from
    ``record_human_blocker`` / ``agent_requires_human`` MCP, stored on the
    work session hygiene blocker (and optionally mirrored on the task).
    """
    session = _map(snapshot.get("work_session"))
    if _text(session.get("status")) == "blocked":
        blocker = _map(_map(session.get("hygiene")).get("blocker"))
        if _text(blocker.get("route")) in _AGENT_HUMAN_ROUTES:
            return True
        if _text(blocker.get("schema")).endswith("human_blocker.v1"):
            return True
        if blocker.get("source_tool") in {
            "record_human_blocker", "agent_requires_human",
        }:
            return True
    task = _map(snapshot.get("task"))
    task_blocker = _map(task.get("human_blocker") or task.get("agent_requires_human"))
    if task_blocker and _text(task_blocker.get("route")) in _AGENT_HUMAN_ROUTES:
        return True
    # Sticky completion attention created by agent closeout (not orchestrator).
    attention = _map(snapshot.get("agent_requires_human") or snapshot.get("attention"))
    if attention.get("sticky") is True and _text(
        attention.get("source_tool")
    ) in {"record_human_blocker", "agent_requires_human"}:
        return True
    return False


def is_merged(snapshot: Mapping[str, Any]) -> bool:
    provenance = _map(snapshot.get("merge_provenance"))
    pr = _map(snapshot.get("github_pr"))
    queue = _map(snapshot.get("merge_queue"))
    if provenance.get("merged_sha") or provenance.get("canonical_merged_sha"):
        return True
    if _text(pr.get("state")) == "merged" or pr.get("merged") is True:
        return True
    if _text(queue.get("state") or queue.get("status")) in {
        "merged", "complete", "completed",
    }:
        return True
    return False


def live_runner(snapshot: Mapping[str, Any]) -> bool:
    runner = _map(snapshot.get("runner"))
    return bool(runner.get("live"))


def has_pr(snapshot: Mapping[str, Any]) -> bool:
    pr = _map(snapshot.get("github_pr"))
    return bool(pr) and bool(
        snapshot.get("pr_number") or pr.get("number") or snapshot.get("pr_identity")
    )


def needs_implementation(snapshot: Mapping[str, Any]) -> bool:
    """No PR yet and board is not already in review/done."""
    if has_pr(snapshot) or is_merged(snapshot):
        return False
    status = _text(snapshot.get("board_status"))
    if status in {"done", "cancelled", "canceled", "in_review"}:
        return False
    # A named board PR without hydrated GitHub body is not implementation work.
    if _text_raw(snapshot.get("board_pr_identity")) or snapshot.get("board_pr_number"):
        return False
    return True


def pr_closed_unmerged(snapshot: Mapping[str, Any]) -> bool:
    pr = _map(snapshot.get("github_pr"))
    return _text(pr.get("state")) == "closed" and not is_merged(snapshot)


def queue_waiting(snapshot: Mapping[str, Any]) -> bool:
    queue = _map(snapshot.get("merge_queue"))
    state = _text(queue.get("state") or queue.get("status"))
    return state in _QUEUE_WAIT


def is_draft(snapshot: Mapping[str, Any]) -> bool:
    return _map(snapshot.get("github_pr")).get("draft") is True


def ci_pending(snapshot: Mapping[str, Any]) -> bool:
    required = list(snapshot.get("required_status_contexts") or [])
    contexts = _map(snapshot.get("status_contexts"))
    if not required:
        return False
    for name in required:
        row = _map(contexts.get(name))
        state = _text(row.get("conclusion") or row.get("state") or row.get("status"))
        if not row or not state or state in _PENDING:
            return True
    return False


def ci_failed(snapshot: Mapping[str, Any]) -> bool:
    required = list(snapshot.get("required_status_contexts") or [])
    contexts = _map(snapshot.get("status_contexts"))
    for name in required:
        row = _map(contexts.get(name))
        state = _text(row.get("conclusion") or row.get("state") or row.get("status"))
        if state in _FAILED:
            return True
    return False


def merge_conflict(snapshot: Mapping[str, Any]) -> bool:
    pr = _map(snapshot.get("github_pr"))
    merge_state = _text(
        pr.get("mergeStateStatus") or pr.get("mergeable_state") or pr.get("merge_state")
    )
    if merge_state == "dirty" or pr.get("mergeable") is False:
        return True
    for finding in list(snapshot.get("findings") or []):
        if not isinstance(finding, Mapping):
            continue
        code = _text(finding.get("code"))
        if code in {"merge_conflict", "pr_merge_conflict", "conflict_markers"}:
            return True
        if code == "pr_not_mergeable" and finding.get("mergeable") is False:
            return True
    return False


def branch_behind(snapshot: Mapping[str, Any]) -> bool:
    pr = _map(snapshot.get("github_pr"))
    merge_state = _text(
        pr.get("mergeStateStatus") or pr.get("mergeable_state") or pr.get("merge_state")
    )
    return merge_state == "behind"


def changes_requested(snapshot: Mapping[str, Any]) -> bool:
    review = _map(snapshot.get("review"))
    state = _text(review.get("status") or review.get("state") or review.get("verdict"))
    return state in {"changes_requested", "changes"}


def review_passed(snapshot: Mapping[str, Any]) -> bool:
    review = _map(snapshot.get("review"))
    state = _text(review.get("status") or review.get("state") or review.get("verdict"))
    if state not in {"pass", "passed", "approved", "success"}:
        return False
    head = _text(snapshot.get("head_sha")).lower()
    review_head = _text(review.get("head_sha")).lower()
    if head and review_head and head != review_head:
        return False
    return True


def review_needed(snapshot: Mapping[str, Any]) -> bool:
    return not review_passed(snapshot)


def queue_failed(snapshot: Mapping[str, Any]) -> bool:
    queue = _map(snapshot.get("merge_queue"))
    state = _text(queue.get("state") or queue.get("status"))
    if state == "unmergeable":
        return True
    removal = _text(queue.get("last_removal_reason") or queue.get("removal_reason"))
    if (
        not state
        and (
            queue.get("prior_enqueue_verified") is True
            or removal in {"failed_checks", "checks_timed_out"}
        )
    ):
        return True
    if state == "locked" and queue.get("retry_exhausted"):
        return True
    return False


def blocking_findings(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in list(snapshot.get("findings") or []):
        if isinstance(item, Mapping) and item.get("blocking") is not False:
            code = _text(item.get("code"))
            if code in {"draft_pr"}:
                continue
            rows.append(dict(item))
    return rows


#: Gate findings that only mean "CI has not settled yet" — not factory red.
_PENDING_FINDING_CODES = frozenset({
    "missing_required_status_contexts",
    "required_status_pending",
    "required_exact_head_ci_pending",
    "ci_pending",
})


def factory_failure_reason(snapshot: Mapping[str, Any]) -> str:
    """Name the observed factory failure without diagnosing ownership."""
    # Pending required checks are WAIT, not remediation.
    if ci_pending(snapshot):
        return ""
    if pr_closed_unmerged(snapshot):
        return "pr_closed_unmerged"
    if merge_conflict(snapshot):
        return "pr_merge_conflict"
    if branch_behind(snapshot):
        return "pr_branch_behind"
    if changes_requested(snapshot):
        return "review_changes_requested"
    if ci_failed(snapshot):
        # Name infrastructure red distinctly for operators/dossiers, but never
        # route it to a human — Mission Bot still boots remediation.
        required = list(snapshot.get("required_status_contexts") or [])
        contexts = _map(snapshot.get("status_contexts"))
        for name in required:
            row = _map(contexts.get(name))
            state = _text(row.get("conclusion") or row.get("state") or row.get("status"))
            if state not in _FAILED:
                continue
            attribution = _text(
                row.get("failure_attribution") or row.get("attribution")
            )
            if attribution == "infrastructure":
                return "required_ci_infrastructure_failure"
            break
        return "required_exact_head_ci_failed"
    if queue_failed(snapshot):
        queue = _map(snapshot.get("merge_queue"))
        state = _text(queue.get("state") or queue.get("status"))
        if state == "unmergeable":
            return "merge_queue_unmergeable"
        if state == "locked":
            return "merge_queue_locked"
        return "merge_queue_ejected_tip_green"
    findings = [
        row for row in blocking_findings(snapshot)
        if _text(row.get("code")) not in _PENDING_FINDING_CODES
    ]
    if findings:
        return _text(findings[0].get("code")) or "merge_gate_blocked"
    return ""


def factory_failure(snapshot: Mapping[str, Any]) -> bool:
    return bool(factory_failure_reason(snapshot))


def gates_green(snapshot: Mapping[str, Any]) -> bool:
    if not has_pr(snapshot) or is_draft(snapshot) or factory_failure(snapshot):
        return False
    if ci_pending(snapshot):
        return False
    if not review_passed(snapshot):
        return False
    required = list(snapshot.get("required_status_contexts") or [])
    contexts = _map(snapshot.get("status_contexts"))
    for name in required:
        row = _map(contexts.get(name))
        state = _text(row.get("conclusion") or row.get("state") or row.get("status"))
        if not row or state not in _PASS:
            return False
    return True


__all__ = [
    "agent_requires_human",
    "blocking_findings",
    "branch_behind",
    "changes_requested",
    "ci_failed",
    "ci_pending",
    "factory_failure",
    "factory_failure_reason",
    "gates_green",
    "has_pr",
    "is_draft",
    "is_merged",
    "live_runner",
    "merge_conflict",
    "needs_implementation",
    "pr_closed_unmerged",
    "queue_failed",
    "queue_waiting",
    "review_needed",
    "review_passed",
]
