"""Pure exact-head completion assessment.

The hydrator deliberately accepts already-read records.  GitHub, SQL, and runner
adapters own I/O; this module owns the shared normalized contract and the one
precedence-ordered route decision.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence


COMPLETION_SNAPSHOT_SCHEMA = "switchboard.completion_snapshot.v1"
COMPLETION_DECISION_SCHEMA = "switchboard.completion_decision.v1"

_PASS = {"success", "passed", "pass", "ok"}
_POLICY_PASS = _PASS | {"neutral", "skipped"}
_PENDING = {"requested", "queued", "in_progress", "waiting", "pending", "expected"}
_COORD_CI = {"cancelled", "canceled", "stale", "startup_failure"}
_QUEUE_WAIT = {"queued", "awaiting_checks", "mergeable", "locked"}
_TERMINAL_BOARD = {"done", "cancelled", "canceled"}

_HUMAN_FINDINGS = {
    "canonical_repo_missing", "repo_role_cannot_merge", "wrong_target_branch",
    "unknown_policy_profile", "task_not_found", "task_not_backed",
}
_REVIEW_FINDINGS = {
    "review_required", "review_verdict_missing", "review_verdict_stale",
    "missing_review_verdict", "stale_review_verdict",
}
_REMEDIATION_FINDINGS = {
    "conflict_markers", "dirty_work_session", "work_session_preflight_failed",
    "semantic_completion_failed",
}
_COORD_FINDINGS = {
    "github_pr_state_unavailable", "stale_head_sha", "stale_branch",
    "missing_safe_rebase_evidence", "missing_required_status_contexts",
    "external_ci_required", "work_session_not_found",
    "missing_work_session_preflight", "work_session_required",
    "missing_executed_test_run", "missing_ui_playwright_evidence",
}


def _map(value: Any) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _head(record: Mapping[str, Any]) -> str:
    nested = record.get("head")
    if isinstance(nested, Mapping):
        return str(nested.get("sha") or "").strip()
    return str(record.get("head_sha") or record.get("sha") or "").strip()


def _check_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        if any(k in value for k in ("name", "context", "state", "status", "conclusion")):
            return [_map(value)]
        return [{"name": str(k), "state": v} for k, v in value.items()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rows: list[dict[str, Any]] = []
        for item in value:
            rows.extend(_check_rows(item))
        return rows
    return []


def build_completion_snapshot(
    *,
    task: Mapping[str, Any] | None = None,
    github_pr: Mapping[str, Any] | None = None,
    required_status_contexts: Sequence[str] = (),
    status_contexts: Any = None,
    review: Mapping[str, Any] | None = None,
    merge_gate: Mapping[str, Any] | None = None,
    merge_queue: Mapping[str, Any] | None = None,
    work_session: Mapping[str, Any] | None = None,
    runner: Mapping[str, Any] | None = None,
    merge_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize independently hydrated records into the shared snapshot contract."""
    task_row, pr, gate = _map(task), _map(github_pr), _map(merge_gate)
    contexts: dict[str, dict[str, Any]] = {}
    sources = (
        pr.get("status_contexts"), pr.get("statusCheckRollup"), pr.get("checks"),
        gate.get("status_contexts"), status_contexts,
    )
    for source in sources:
        for row in _check_rows(source):
            name = str(row.get("name") or row.get("context") or row.get("check_name") or "").strip()
            if name:
                contexts[name] = row
    required = list(dict.fromkeys(
        str(item).strip() for item in
        [*(gate.get("required_status_contexts") or []), *required_status_contexts]
        if str(item).strip()
    ))
    board_git = _map(task_row.get("git_state"))
    snapshot = {
        "schema": COMPLETION_SNAPSHOT_SCHEMA,
        "task": task_row,
        "task_id": str(task_row.get("task_id") or gate.get("task_id") or "").strip().upper(),
        "board_status": str(task_row.get("status") or "").strip(),
        "board_head_sha": _head(board_git),
        "github_pr": pr,
        "pr_number": pr.get("number") or gate.get("pr_number"),
        "head_sha": _head(pr) or str(gate.get("head_sha") or "").strip(),
        "required_status_contexts": required,
        "status_contexts": contexts,
        "review": _map(review) or _map(gate.get("review_gate")),
        # Keep merge_gate's existing coded findings as the finding authority.
        "merge_gate": gate,
        "findings": deepcopy(list(gate.get("findings") or [])),
        "merge_queue": _map(merge_queue),
        "work_session": _map(work_session),
        "runner": _map(runner),
        "merge_provenance": _map(merge_provenance),
    }
    return snapshot


def _decision(state: str, route: str, reason: str, *, role: str | None = None,
              board: str = "In Review", retry: str = "none",
              effect: str = "none") -> dict[str, Any]:
    return {
        "schema": COMPLETION_DECISION_SCHEMA,
        "state": state,
        "route": route,
        "reason_code": reason,
        "desired_role": role,
        "retry_policy": retry,
        "board_projection": board,
        "effect": effect,
    }


def _finding_decision(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Map merge_gate codes; never infer a route from aggregate PR findings."""
    for finding in findings:
        if finding.get("blocking") is False:
            continue
        code = _text(finding.get("code"))
        failure = _text(finding.get("failure_class"))
        kind = _text(finding.get("finding_class") or finding.get("kind"))
        if code in {"draft_pr", "pr_not_mergeable"}:
            # Both are aggregate/derived gate findings. Draft is evaluated only
            # after substantive evidence; mergeability is decomposed below.
            continue
        if code in _HUMAN_FINDINGS or failure in {"absent_permission", "invalid_input"}:
            return _decision("blocked", "human", code or failure, board="Blocked")
        if code in _REVIEW_FINDINGS:
            return _decision("assessing", "review_merge", code, role="review_merge")
        if (
            code in _REMEDIATION_FINDINGS
            or kind in {"automatic", "product", "code"}
            or failure == "hidden_fallback"
        ):
            return _decision("blocked", "remediation", code or failure,
                             role="remediation", board="Blocked")
        if kind in {"judgment", "authority", "policy", "human"}:
            return _decision("blocked", "human", code or kind, board="Blocked")
        if code in _COORD_FINDINGS or failure in {
            "broken_connection", "missing_data", "stale_branch", "unreachable_agent",
        }:
            return _decision("blocked", "coordination_retry", code or failure,
                             retry="bounded")
        if failure == "failed_gate":
            return _decision("blocked", "human", code or "unclassified_failed_gate",
                             board="Blocked")
    return None


def classify_completion(
    current_run: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one deterministic, side-effect-free decision for an exact-head snapshot."""
    del current_run  # reserved for retry-budget/state-version policy; never mutated
    snap = _map(snapshot)
    task_status = _text(snap.get("board_status") or _map(snap.get("task")).get("status"))
    provenance = _map(snap.get("merge_provenance"))
    pr = _map(snap.get("github_pr"))
    queue = _map(snap.get("merge_queue"))
    review = _map(snap.get("review"))
    runner = _map(snap.get("runner"))
    head_sha = str(snap.get("head_sha") or _head(pr)).strip()
    board_head = str(snap.get("board_head_sha") or "").strip()

    if task_status in _TERMINAL_BOARD and (
        task_status != "done" or provenance.get("merged_sha")
    ):
        return _decision("done" if task_status == "done" else "cancelled", "none",
                         "terminal_provenance_valid", board="Done" if task_status == "done" else "Cancelled")
    pr_state = _text(pr.get("state"))
    if provenance.get("merged_sha") or pr_state == "merged" or pr.get("merged") is True:
        return _decision("reconciling", "reconcile", "canonical_pr_merged")
    if pr_state == "closed":
        route = "coordination_retry" if pr.get("reopen_authorized") is True else "human"
        return _decision("blocked", route, "pr_closed_unmerged",
                         retry="bounded" if route == "coordination_retry" else "none",
                         board="In Review" if route == "coordination_retry" else "Blocked")
    if not pr or not head_sha:
        return _decision("blocked", "coordination_retry", "exact_head_pr_missing",
                         retry="bounded")
    if board_head and board_head != head_sha:
        return _decision("blocked", "coordination_retry", "board_pr_head_mismatch",
                         retry="bounded")

    queue_state = _text(queue.get("state") or queue.get("status"))
    queue_failure = _text(queue.get("failure_attribution") or queue.get("failure_class"))
    if queue_state in {"merged", "complete", "completed"}:
        return _decision("reconciling", "reconcile", "merge_queue_merged")
    if queue_state in _QUEUE_WAIT:
        if queue_state == "locked" and queue.get("retry_exhausted"):
            return _decision("blocked", "coordination_retry", "merge_queue_locked",
                             retry="bounded")
        return _decision("waiting_merge_queue", "wait", f"merge_queue_{queue_state}",
                         retry="bounded")
    if queue_state == "unmergeable":
        if queue_failure in {"product", "conflict"}:
            return _decision("blocked", "remediation", "merge_queue_product_failure",
                             role="remediation", board="Blocked")
        if queue_failure in {"policy", "authority"}:
            return _decision("blocked", "human", "merge_queue_authority_failure",
                             board="Blocked")
        return _decision("blocked", "coordination_retry", "merge_queue_infrastructure_failure",
                         retry="bounded")

    required = list(snap.get("required_status_contexts") or [])
    contexts = _map(snap.get("status_contexts"))
    for name in required:
        row = _map(contexts.get(name))
        state = _text(row.get("conclusion") or row.get("state") or row.get("status"))
        attribution = _text(row.get("failure_attribution") or row.get("failure_class") or "unknown")
        if not row or not state:
            reason = "required_ci_missing" if snap.get("ci_dispatch_recent") else "required_ci_hydration_missing"
            route = "wait" if snap.get("ci_dispatch_recent") else "coordination_retry"
            return _decision("waiting" if route == "wait" else "blocked", route, reason,
                             retry="bounded")
        if state in _PENDING:
            return _decision("waiting", "wait", "required_exact_head_ci_pending",
                             retry="bounded")
        if state in _COORD_CI:
            return _decision("blocked", "coordination_retry",
                             f"required_ci_{state}", retry="bounded")
        if state in {"failure", "failed", "error", "timed_out", "action_required"}:
            if state == "action_required" or attribution in {"policy", "authority"}:
                return _decision("blocked", "human", "required_ci_authority_failure",
                                 board="Blocked")
            if attribution == "product":
                return _decision("blocked", "remediation", "required_exact_head_ci_failed",
                                 role="remediation", board="Blocked")
            if attribution == "infrastructure" or state == "timed_out":
                return _decision("blocked", "coordination_retry",
                                 "required_ci_infrastructure_failure", retry="bounded")
            return _decision("blocked", "human", "required_ci_failure_unknown",
                             board="Blocked")
        if state not in _POLICY_PASS:
            return _decision("blocked", "coordination_retry", "required_ci_state_unknown",
                             retry="bounded")

    review_state = _text(
        review.get("status") or review.get("state") or review.get("verdict")
    )
    review_head = str(review.get("head_sha") or "").strip()
    if review.get("retry_exhausted"):
        return _decision("blocked", "human", "review_retry_budget_exhausted", board="Blocked")
    if review_state in {"changes_requested", "changes"}:
        findings = [_map(item) for item in (review.get("findings") or [])]
        automatic = [
            item for item in findings
            if _text(item.get("finding_class") or item.get("kind") or item.get("class"))
            in {"automatic", "product", "code", "auto"}
        ]
        escalated = [item for item in findings if item not in automatic]
        if automatic:
            # Mixed findings dispatch the automatic repair AND keep the
            # escalation. One judgment finding must not strand work a coder
            # could have fixed (COORD-46 normalization requirement 3).
            decision = _decision("blocked", "remediation", "automatic_review_findings",
                                 role="remediation", board="Blocked")
            decision["acceptance_findings"] = automatic
            decision["escalated_findings"] = escalated
            return decision
        decision = _decision("blocked", "human", "human_review_findings", board="Blocked")
        decision["escalated_findings"] = escalated
        return decision
    review_passed = review_state in {"pass", "passed", "approved", "success"}
    if not review_passed or (review_head and review_head != head_sha):
        return _decision("assessing", "review_merge",
                         "review_verdict_stale" if review_passed else "review_required",
                         role="review_merge")

    finding_route = _finding_decision(snap.get("findings") or [])
    if finding_route:
        return finding_route

    mergeable = pr.get("mergeable")
    merge_state = _text(
        pr.get("mergeStateStatus") or pr.get("mergeable_state") or pr.get("merge_state")
    )
    if mergeable is False or merge_state in {"dirty", "conflicting"}:
        return _decision("blocked", "remediation", "pr_merge_conflict",
                         role="remediation", board="Blocked")
    if merge_state == "behind":
        return _decision("blocked", "coordination_retry", "pr_branch_behind",
                         retry="bounded")
    if merge_state == "unknown" or mergeable is None:
        route = "coordination_retry" if snap.get("mergeability_retry_exhausted") else "wait"
        return _decision("blocked" if route != "wait" else "waiting", route,
                         "pr_mergeability_unknown", retry="bounded")
    # BLOCKED and UNSTABLE have now been decomposed through exact-head CI,
    # review, findings, and conflicts. They are never decisions by themselves.

    if pr.get("draft") is True:
        return _decision("ready_to_queue", "review_merge", "draft_ready_to_mark_ready",
                         role="review_merge", effect="mark_ready_then_reread")

    desired_role = None
    runner_role = _text(runner.get("role") or runner.get("execution_role"))
    runner_head = str(runner.get("head_sha") or "").strip()
    if runner and runner.get("live") and (runner_role != desired_role or runner_head != head_sha):
        # A clean snapshot needs no role runner; stale live execution must be fenced
        # before queueing, but remains an orchestration repair rather than coding work.
        return _decision("blocked", "coordination_retry", "live_runner_not_desired",
                         retry="bounded", effect="fence_runner")
    return _decision("ready_to_queue", "review_merge", "exact_head_gates_passed",
                     role="review_merge", effect="enqueue")
