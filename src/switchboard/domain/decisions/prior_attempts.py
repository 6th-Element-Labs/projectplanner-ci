"""Bounded prior-attempt memory for the execution assignment (COORD-52).

Implements §7 of docs/DECISION-CORPUS-OUTCOMES-SPEC.md. The live CO-20 assignment was
``{reason_code, route, acceptance_findings: [], generation, exact_head_sha}`` — no history
at all, so seven consecutive remediations each believed they were the first. That is
amnesia rather than misclassification, and no retry budget fixes it: attempt seven has no
reason to behave differently from attempt one.

This module is pure. It reads a task's decision episodes (COORD-50's corpus, closed with
outcomes and joined to executions by COORD-51) and returns the summary a dispatch should
carry. Purity matters here beyond testability — see the warning below.

WHY THIS IS DERIVED ONCE AND THEN ECHOED, NEVER RE-DERIVED
``claims.py`` re-runs ``build_execution_assignment`` at claim time and compares the result
to the stored contract with exact dict equality (``require_exact_execution_assignment``).
The corpus is append-only and moves constantly, so a block derived from live episodes would
differ between dispatch and claim and fail EVERY claim with
``execution_assignment_contract_mismatch``. Dispatch therefore derives this block once and
stores it; the claim path passes the stored value straight back in. Treat the block as
immutable assignment content, exactly like ``exact_head_sha``.

BOUNDED BY CONSTRUCTION
A summary, never a transcript dump (transcripts are SIMPLIFY-9). Vectors are capped and
totals are reported separately, so a task with two hundred episodes still produces a
fixed-size payload.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional


SCHEMA = "switchboard.prior_attempts.v1"

#: Cap every vector. Totals live in the scalar counters, so truncation loses ordering
#: detail and never loses magnitude.
MAX_OUTCOMES = 8
MAX_ROUTES = 4
MAX_EXECUTION_IDS = 3

#: Reason codes whose history is not trustworthy enough to hand a worker. Feeding a
#: mislabelled history is worse than feeding none — the worker acts confidently on a false
#: pattern. BUG-182 (``exact_head_pr_missing`` firing for PRs that are open at that exact
#: head, masking merge conflicts as an infra retry) merged as 90b9d4c3, so that code is no
#: longer excluded. Kept as a live seam: add a code here the moment its emitter is
#: suspect, rather than silently shipping a lie.
UNTRUSTED_REASON_CODES: frozenset[str] = frozenset()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _evidence_session_id(episodes: list[Mapping[str, Any]]) -> str:
    """The work session holding the newest attempt's near-miss evidence (COORD-61).

    Counts alone would not have rescued the CO-21 repair dispatch: it needed to know WHERE
    the previous attempt's facts already lived. COORD-61 records that as
    ``features.missing_artifact.found_near_miss[].work_session_id``; surface the newest one
    so a repair amends the bound session instead of forking a fresh one.
    """
    for episode in reversed(episodes):
        features = episode.get("features")
        if not isinstance(features, Mapping):
            continue
        artifact = features.get("missing_artifact")
        if not isinstance(artifact, Mapping):
            continue
        for near_miss in artifact.get("found_near_miss") or []:
            if not isinstance(near_miss, Mapping):
                continue
            session_id = _text(near_miss.get("work_session_id"))
            if session_id:
                return session_id
    return ""


def build_prior_attempts(
    episodes: Iterable[Mapping[str, Any]],
    *,
    reason_code: str,
    head_sha: str = "",
) -> Optional[dict[str, Any]]:
    """Summarize what has already been tried for this reason code on this task.

    ``episodes`` is the task's corpus timeline, oldest first, as
    ``decision_records.list_decision_episodes`` returns it. Returns ``None`` for a
    first-ever dispatch — the assignment omits the field entirely rather than carrying a
    zeroed object that reads as "we tried nothing" instead of "we have no history".
    """
    reason_code = _text(reason_code)
    if not reason_code or reason_code in UNTRUSTED_REASON_CODES:
        return None

    head_sha = _text(head_sha).lower()
    prior = [
        dict(episode) for episode in episodes or []
        if _text(episode.get("reason_code")) == reason_code
    ]
    if not prior:
        return None

    on_this_head = [
        episode for episode in prior
        if head_sha and _text(episode.get("head_sha")).lower() == head_sha
    ]

    routes: list[str] = []
    for episode in prior:
        route = _text(episode.get("route"))
        if route and route not in routes:
            routes.append(route)

    execution_ids: list[str] = []
    for episode in reversed(prior):
        execution_id = _text(episode.get("execution_id"))
        if execution_id and execution_id not in execution_ids:
            execution_ids.append(execution_id)
        if len(execution_ids) >= MAX_EXECUTION_IDS:
            break

    # The head has moved if any prior attempt recorded it moving, or if the oldest attempt
    # for this code was made against a different head than the one being dispatched now.
    first_head = _text(prior[0].get("head_sha")).lower()
    head_advanced_since_first = bool(
        any(episode.get("head_advanced") for episode in prior)
        or (head_sha and first_head and first_head != head_sha)
    )

    summary: dict[str, Any] = {
        "schema": SCHEMA,
        "attempt_number": len(prior) + 1,
        "same_reason_code_on_this_head": len(on_this_head),
        "reason_code": reason_code,
        "routes_tried": routes[:MAX_ROUTES],
        # Newest last, matching the episode timeline's own ordering.
        "outcomes": [
            _text(episode.get("outcome")) or "open"
            for episode in prior[-MAX_OUTCOMES:]
        ],
        "head_advanced_since_first": head_advanced_since_first,
        "generations_spent": sum(
            _int(episode.get("generations_spent")) for episode in prior),
        "recent_execution_ids": execution_ids,
        "total_prior_episodes": len(prior),
        "truncated": len(prior) > MAX_OUTCOMES or len(routes) > MAX_ROUTES,
    }

    evidence_session_id = _evidence_session_id(prior)
    if evidence_session_id:
        summary["evidence_session_id"] = evidence_session_id
    return summary


__all__ = [
    "SCHEMA",
    "MAX_OUTCOMES",
    "MAX_ROUTES",
    "MAX_EXECUTION_IDS",
    "UNTRUSTED_REASON_CODES",
    "build_prior_attempts",
]
