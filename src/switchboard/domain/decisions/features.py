"""``switchboard.decision_features.v1`` — the projection allowlist (spec §4.2).

The exhaustive set of fields derivable from a ``switchboard.completion_snapshot.v1``
that may cross a tenant boundary. Every value is a bool, a small closed enum, or a
bucketed integer, and every field describes the *shape* of the snapshot, never its
content.

Deliberately excluded and unreachable from here: task titles and descriptions,
repository and branch names, status-context names, head SHAs, PR numbers and URLs,
agent and host identity, and any raw count that reveals throughput.

Spec §6.1 is the load-bearing rule: this projection is materialized at *write* time,
never derived at export time. Widening it is a migration, not a query change. Any
value outside a declared enum collapses to ``"other"`` so an unexpected upstream
string can never leak through as content.
"""
from __future__ import annotations

from typing import Any, Mapping


DECISION_FEATURES_SCHEMA = "switchboard.decision_features.v1"
FEATURES_VERSION = "switchboard.decision_features.v1"

FEATURE_FIELDS: tuple[str, ...] = (
    "board_status",
    "has_pr",
    "pr_state",
    "pr_draft",
    "pr_mergeable_state",
    "board_pr_identity_matches",
    "required_contexts_count",
    "contexts_fully_hydrated",
    "any_required_context_failed",
    "any_required_context_pending",
    "any_required_context_cancelled",
    "review_verdict_present",
    "review_verdict_status",
    "review_verdict_stale",
    "open_blocking_findings_count",
    "merge_queue_state",
    "work_session_present",
    "work_session_preflight_state",
    "runner_live",
    "runner_role_matches_desired",
    "runner_head_matches_exact_head",
    "has_merge_provenance",
    "generation_bucket",
)

OTHER = "other"
NONE = "none"

_BOARD_STATUS = frozenset({
    "not_started", "in_progress", "in_review", "blocked", "done", "cancelled",
})
_PR_STATE = frozenset({"open", "closed", "merged"})
_MERGEABLE_STATE = frozenset({
    "clean", "dirty", "behind", "blocked", "unstable", "draft", "has_hooks",
    "unknown", "conflicting",
})
_REVIEW_STATUS = frozenset({
    "passed", "pass", "approved", "success", "changes_requested", "failed",
    "pending",
})
_QUEUE_STATE = frozenset({
    "queued", "awaiting_checks", "mergeable", "locked", "unmergeable", "merged",
})
_PREFLIGHT_STATE = frozenset({"passed", "failed", "pending", "skipped", "unknown"})

_PASS_CONTEXT = frozenset({"success", "passed", "pass", "ok", "neutral", "skipped"})
_PENDING_CONTEXT = frozenset({
    "requested", "queued", "in_progress", "waiting", "pending", "expected",
})
_FAILED_CONTEXT = frozenset({
    "failure", "failed", "error", "timed_out", "action_required",
})
_CANCELLED_CONTEXT = frozenset({"cancelled", "canceled", "stale", "startup_failure"})


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _enum(value: Any, allowed: frozenset[str]) -> str:
    """Collapse to a declared enum member, ``none`` when absent, else ``other``."""
    text = _text(value)
    if not text:
        return NONE
    return text if text in allowed else OTHER


def _context_bucket(count: int) -> str:
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 3:
        return "2-3"
    if count <= 6:
        return "4-6"
    return "7+"


def _findings_bucket(count: int) -> str:
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 3:
        return "2-3"
    return "4+"


def _generation_bucket(generation: int) -> str:
    if generation <= 1:
        return "1"
    if generation == 2:
        return "2"
    if generation <= 5:
        return "3-5"
    return "6+"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _context_states(snapshot: Mapping[str, Any]) -> list[str]:
    """One normalized state per *required* context, ``""`` when unhydrated."""
    contexts = _map(snapshot.get("status_contexts"))
    states: list[str] = []
    for name in list(snapshot.get("required_status_contexts") or []):
        row = _map(contexts.get(name))
        states.append(_text(
            row.get("conclusion") or row.get("state") or row.get("status")
        ) if row else "")
    return states


def _blocking_findings(snapshot: Mapping[str, Any]) -> int:
    count = 0
    for finding in list(snapshot.get("findings") or []):
        if isinstance(finding, Mapping) and finding.get("blocking") is not False:
            count += 1
    return count


def project_features(
    snapshot: Mapping[str, Any],
    decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize ``switchboard.decision_features.v1`` for one classified snapshot.

    ``decision`` supplies ``desired_role`` — the only comparison target that is not
    itself in the snapshot. The result carries exactly :data:`FEATURE_FIELDS`, in
    declared order, and nothing else.
    """
    snap = _map(snapshot)
    verdict = _map(decision)
    pr = _map(snap.get("github_pr"))
    review = _map(snap.get("review"))
    queue = _map(snap.get("merge_queue"))
    runner = _map(snap.get("runner"))
    session = _map(snap.get("work_session"))
    provenance = _map(snap.get("merge_provenance"))

    head_sha = str(snap.get("head_sha") or "").strip().lower()
    states = _context_states(snap)
    review_status = _text(
        review.get("status") or review.get("state") or review.get("verdict")
    )
    desired_role = _text(verdict.get("desired_role"))
    runner_role = _text(runner.get("role") or runner.get("execution_role"))
    board_identity = str(snap.get("board_pr_identity") or "").strip().lower()
    pr_identity = str(snap.get("pr_identity") or "").strip().lower()

    merged = bool(provenance.get("merged_sha")) or pr.get("merged") is True
    pr_state = "merged" if merged else _text(pr.get("state"))

    features = {
        "board_status": _enum(snap.get("board_status"), _BOARD_STATUS),
        "has_pr": bool(pr) and _int(snap.get("pr_number") or pr.get("number")) > 0,
        "pr_state": _enum(pr_state, _PR_STATE),
        "pr_draft": pr.get("draft") is True,
        "pr_mergeable_state": _enum(
            pr.get("mergeStateStatus") or pr.get("mergeable_state")
            or pr.get("merge_state"),
            _MERGEABLE_STATE,
        ),
        # Absent board identity is not a mismatch: nothing contradicts the PR yet.
        "board_pr_identity_matches": (
            not board_identity or board_identity == pr_identity
        ),
        "required_contexts_count": _context_bucket(len(states)),
        "contexts_fully_hydrated": bool(states) and all(states),
        "any_required_context_failed": any(s in _FAILED_CONTEXT for s in states),
        "any_required_context_pending": any(s in _PENDING_CONTEXT for s in states),
        "any_required_context_cancelled": any(
            s in _CANCELLED_CONTEXT for s in states
        ),
        "review_verdict_present": bool(review),
        "review_verdict_status": _enum(review_status, _REVIEW_STATUS),
        "review_verdict_stale": bool(review.get("invalidated_by_head_sha")),
        "open_blocking_findings_count": _findings_bucket(_blocking_findings(snap)),
        "merge_queue_state": _enum(
            queue.get("state") or queue.get("status"), _QUEUE_STATE,
        ),
        "work_session_present": bool(session),
        "work_session_preflight_state": _enum(
            session.get("preflight_state") or session.get("preflight_status"),
            _PREFLIGHT_STATE,
        ),
        "runner_live": bool(runner.get("live")),
        "runner_role_matches_desired": runner_role == desired_role,
        "runner_head_matches_exact_head": (
            str(runner.get("head_sha") or "").strip().lower() == head_sha
            and bool(head_sha)
        ),
        "has_merge_provenance": merged,
        "generation_bucket": _generation_bucket(_int(runner.get("generation"))),
    }
    # Declared order, declared membership. A field added above but not to
    # FEATURE_FIELDS would silently widen the projection, so build from the tuple.
    return {name: features[name] for name in FEATURE_FIELDS}


__all__ = [
    "DECISION_FEATURES_SCHEMA",
    "FEATURES_VERSION",
    "FEATURE_FIELDS",
    "NONE",
    "OTHER",
    "project_features",
]
