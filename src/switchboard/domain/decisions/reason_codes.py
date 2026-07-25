"""``switchboard.reason_code.v1`` — the reason-code registry (spec §4.1).

``reason_code`` was a free string emitted by the completion state machine with no
owner, no enum, and no test asserting an emitted code is known. Two spellings of the
same event (``required_ci_cancelled`` / ``required_ci_canceled``) both reached the
board and would silently split a cohort in any count.

This module gives the vocabulary an owner:

* every code the completion authority may emit is declared once, with the coarse
  ``family``, the single ``subsystem`` permitted to emit it, whether it is
  ``transient`` or ``anomalous``, who can clear it, and whether it may cross a
  tenant boundary in an exported projection;
* :func:`canonical_reason_code` folds known spelling variants onto one code so the
  corpus counts one cohort rather than two;
* :func:`is_registered` makes an unregistered code *visible* — the corpus reports it
  rather than accepting it as anonymous free text.

The registry is **additive only**. Codes are API: they are never renamed in place.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


REASON_CODE_SCHEMA = "switchboard.reason_code.v1"

FAMILIES = frozenset({
    "required_ci", "review", "merge_queue", "mergeability",
    "placement", "provider", "runner", "terminal", "session",
})
EXPECTED = frozenset({"transient", "anomalous"})
RESOLVERS = frozenset({"agent", "human", "infra", "provider"})

# Spelling variants that mean the same event. Applied before any count so one event
# cannot present as two cohorts. Additive only, and asserted collision-free by test.
_SPELLING_ALIASES = {
    "canceled": "cancelled",
}


@dataclass(frozen=True)
class ReasonCode:
    """One declared entry in ``switchboard.reason_code.v1``."""

    code: str
    family: str
    subsystem: str
    expected: str
    resolver: str
    poolable: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": REASON_CODE_SCHEMA,
            "code": self.code,
            "family": self.family,
            "subsystem": self.subsystem,
            "expected": self.expected,
            "resolver": self.resolver,
            "poolable": self.poolable,
        }


def _code(code: str, family: str, subsystem: str, expected: str,
          resolver: str, poolable: bool = True) -> ReasonCode:
    if family not in FAMILIES:
        raise ValueError(f"unknown reason-code family: {family}")
    if expected not in EXPECTED:
        raise ValueError(f"unknown reason-code expectation: {expected}")
    if resolver not in RESOLVERS:
        raise ValueError(f"unknown reason-code resolver: {resolver}")
    return ReasonCode(code, family, subsystem, expected, resolver, poolable)


_COMPLETION = "completion_state_machine"
_MERGE_GATE = "merge_gate"

_ENTRIES: tuple[ReasonCode, ...] = (
    # -- terminal / provenance -------------------------------------------------
    _code("terminal_provenance_valid", "terminal", _COMPLETION, "transient", "agent"),
    _code("canonical_pr_merged", "terminal", _COMPLETION, "transient", "agent"),
    _code("merge_queue_merged", "terminal", _COMPLETION, "transient", "agent"),
    _code("pr_closed_unmerged", "terminal", _COMPLETION, "anomalous", "human"),
    _code("recovered_incomplete_run", "terminal", "completion_runs", "anomalous", "agent"),
    # -- exact-head identity ---------------------------------------------------
    _code("exact_head_pr_missing", "mergeability", _COMPLETION, "anomalous", "infra"),
    _code("exact_pr_identity_missing", "mergeability", _COMPLETION, "anomalous", "infra"),
    _code("board_pr_identity_mismatch", "mergeability", _COMPLETION, "anomalous", "agent"),
    _code("board_pr_head_mismatch", "mergeability", _COMPLETION, "transient", "agent"),
    # -- required CI -----------------------------------------------------------
    _code("required_ci_missing", "required_ci", _COMPLETION, "transient", "infra"),
    _code("required_ci_hydration_missing", "required_ci", _COMPLETION, "anomalous", "infra"),
    _code("required_exact_head_ci_pending", "required_ci", _COMPLETION, "transient", "infra"),
    _code("required_exact_head_ci_failed", "required_ci", _COMPLETION, "anomalous", "agent"),
    _code("required_ci_cancelled", "required_ci", _COMPLETION, "transient", "infra"),
    _code("required_ci_stale", "required_ci", _COMPLETION, "transient", "infra"),
    _code("required_ci_startup_failure", "required_ci", _COMPLETION, "anomalous", "infra"),
    _code("required_ci_authority_failure", "required_ci", _COMPLETION, "anomalous", "human"),
    _code("required_ci_infrastructure_failure", "required_ci", _COMPLETION, "transient", "infra"),
    _code("required_ci_failure_unknown", "required_ci", _COMPLETION, "anomalous", "human"),
    _code("required_ci_state_unknown", "required_ci", _COMPLETION, "anomalous", "infra"),
    # -- merge queue -----------------------------------------------------------
    _code("merge_queue_queued", "merge_queue", _COMPLETION, "transient", "infra"),
    _code("merge_queue_awaiting_checks", "merge_queue", _COMPLETION, "transient", "infra"),
    _code("merge_queue_mergeable", "merge_queue", _COMPLETION, "transient", "infra"),
    _code("merge_queue_locked", "merge_queue", _COMPLETION, "anomalous", "infra"),
    _code("merge_queue_product_failure", "merge_queue", _COMPLETION, "anomalous", "agent"),
    _code("merge_queue_authority_failure", "merge_queue", _COMPLETION, "anomalous", "human"),
    _code("merge_queue_infrastructure_failure", "merge_queue", _COMPLETION, "transient", "infra"),
    # -- review ----------------------------------------------------------------
    _code("review_required", "review", _COMPLETION, "transient", "agent"),
    _code("review_verdict_stale", "review", _COMPLETION, "transient", "agent"),
    _code("review_verdict_missing", "review", _MERGE_GATE, "transient", "agent"),
    _code("missing_review_verdict", "review", _MERGE_GATE, "transient", "agent"),
    _code("stale_review_verdict", "review", _MERGE_GATE, "transient", "agent"),
    _code("review_retry_budget_exhausted", "review", _COMPLETION, "anomalous", "human"),
    _code("automatic_review_findings", "review", _COMPLETION, "transient", "agent"),
    _code("human_review_findings", "review", _COMPLETION, "anomalous", "human"),
    # -- mergeability ----------------------------------------------------------
    _code("pr_merge_conflict", "mergeability", _COMPLETION, "transient", "agent"),
    _code("pr_branch_behind", "mergeability", _COMPLETION, "transient", "agent"),
    _code("pr_mergeability_unknown", "mergeability", _COMPLETION, "transient", "infra"),
    _code("draft_ready_to_mark_ready", "mergeability", _COMPLETION, "transient", "agent"),
    _code("exact_head_gates_passed", "mergeability", _COMPLETION, "transient", "agent"),
    # -- runner ----------------------------------------------------------------
    _code("live_runner_not_desired", "runner", _COMPLETION, "transient", "agent"),
    # -- merge-gate findings promoted to a route -------------------------------
    _code("canonical_repo_missing", "placement", _MERGE_GATE, "anomalous", "human", False),
    _code("repo_role_cannot_merge", "placement", _MERGE_GATE, "anomalous", "human", False),
    _code("wrong_target_branch", "placement", _MERGE_GATE, "anomalous", "human", False),
    _code("unknown_policy_profile", "placement", _MERGE_GATE, "anomalous", "human"),
    _code("task_not_found", "placement", _MERGE_GATE, "anomalous", "human"),
    _code("task_not_backed", "placement", _MERGE_GATE, "anomalous", "human"),
    _code("github_pr_state_unavailable", "provider", _MERGE_GATE, "transient", "provider"),
    _code("stale_head_sha", "mergeability", _MERGE_GATE, "transient", "agent"),
    _code("stale_branch", "mergeability", _MERGE_GATE, "transient", "agent"),
    _code("missing_safe_rebase_evidence", "mergeability", _MERGE_GATE, "transient", "agent"),
    _code("missing_required_status_contexts", "required_ci", _MERGE_GATE, "anomalous", "infra"),
    _code("external_ci_required", "required_ci", _MERGE_GATE, "transient", "infra"),
    _code("conflict_markers", "session", _MERGE_GATE, "anomalous", "agent"),
    _code("dirty_work_session", "session", _MERGE_GATE, "anomalous", "agent"),
    _code("work_session_preflight_failed", "session", _MERGE_GATE, "anomalous", "agent"),
    _code("semantic_completion_failed", "session", _MERGE_GATE, "anomalous", "agent"),
    _code("work_session_not_found", "session", _MERGE_GATE, "anomalous", "agent"),
    _code("missing_work_session_preflight", "session", _MERGE_GATE, "transient", "agent"),
    _code("work_session_required", "session", _MERGE_GATE, "transient", "agent"),
    _code("missing_executed_test_run", "session", _MERGE_GATE, "transient", "agent"),
    _code("missing_ui_playwright_evidence", "session", _MERGE_GATE, "transient", "agent"),
    _code("unclassified_failed_gate", "placement", _MERGE_GATE, "anomalous", "human"),
    # Failure classes and finding kinds reach the route when a gate finding carries
    # no code of its own. They are part of the emitted vocabulary and are declared
    # here so a bare failure class is never mistaken for an unregistered code.
    _code("absent_permission", "placement", _MERGE_GATE, "anomalous", "human"),
    _code("invalid_input", "placement", _MERGE_GATE, "anomalous", "agent"),
    _code("hidden_fallback", "placement", _MERGE_GATE, "anomalous", "agent"),
    _code("broken_connection", "provider", _MERGE_GATE, "transient", "infra"),
    _code("missing_data", "placement", _MERGE_GATE, "transient", "agent"),
    _code("unreachable_agent", "runner", _MERGE_GATE, "transient", "infra"),
    _code("failed_gate", "placement", _MERGE_GATE, "anomalous", "human"),
    _code("automatic", "placement", _MERGE_GATE, "transient", "agent"),
    _code("product", "placement", _MERGE_GATE, "transient", "agent"),
    _code("code", "placement", _MERGE_GATE, "transient", "agent"),
    _code("judgment", "placement", _MERGE_GATE, "anomalous", "human"),
    _code("authority", "placement", _MERGE_GATE, "anomalous", "human"),
    _code("policy", "placement", _MERGE_GATE, "anomalous", "human"),
    _code("human", "placement", _MERGE_GATE, "anomalous", "human"),
)

REASON_CODES: dict[str, ReasonCode] = {entry.code: entry for entry in _ENTRIES}

# Codes that assert the retry budget is spent, not merely that a retry is pending.
# COORD-51 §3.1 closes the episodes they classify as ``abandoned``: the loop stopped
# because it ran out of budget, which is a different outcome from a head that moved on.
#
# Deliberately narrow. ``merge_queue_locked`` and ``pr_mergeability_unknown`` are
# emitted *while* a bounded retry is still live, so counting them as abandoned would
# label a working loop as given-up. Adding a code here is a one-line change and should
# be made only for a code whose emission means no further attempt will be made.
RETRY_BUDGET_EXHAUSTED_CODES = frozenset({
    "review_retry_budget_exhausted",
})


def spelling_key(code: str) -> str:
    """Fold a code onto its canonical spelling family.

    Two codes with the same key differ only by spelling and must never both be
    registered — otherwise one event silently counts as two cohorts.
    """
    parts = [
        _SPELLING_ALIASES.get(part, part)
        for part in str(code or "").strip().lower().split("_")
    ]
    return "_".join(part for part in parts if part)


def canonical_reason_code(code: str) -> str:
    """Return the registered spelling for ``code``.

    Unknown codes are returned normalized but unchanged — the corpus records them
    verbatim and flags them as unregistered rather than inventing a home for them.
    """
    normalized = str(code or "").strip()
    if not normalized:
        return ""
    if normalized in REASON_CODES:
        return normalized
    folded = spelling_key(normalized)
    return folded if folded in REASON_CODES else normalized


def get_reason_code(code: str) -> Optional[ReasonCode]:
    return REASON_CODES.get(canonical_reason_code(code))


def is_registered(code: str) -> bool:
    return canonical_reason_code(code) in REASON_CODES


def is_retry_budget_exhausted(code: str) -> bool:
    """Report whether ``code`` means no further attempt will be made."""
    return canonical_reason_code(code) in RETRY_BUDGET_EXHAUSTED_CODES


__all__ = [
    "EXPECTED",
    "FAMILIES",
    "REASON_CODES",
    "REASON_CODE_SCHEMA",
    "RESOLVERS",
    "RETRY_BUDGET_EXHAUSTED_CODES",
    "is_retry_budget_exhausted",
    "ReasonCode",
    "canonical_reason_code",
    "get_reason_code",
    "is_registered",
    "spelling_key",
]
