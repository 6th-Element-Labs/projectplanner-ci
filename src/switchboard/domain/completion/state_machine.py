"""Pure exact-head completion assessment.

The hydrator deliberately accepts already-read records.  GitHub, SQL, and runner
adapters own I/O; this module owns the shared normalized contract and the one
precedence-ordered route decision.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Mapping, Sequence


COMPLETION_SNAPSHOT_SCHEMA = "switchboard.completion_snapshot.v1"
COMPLETION_DECISION_SCHEMA = "switchboard.completion_decision.v1"
# Stamped on every decision record so a replay (spec §8.2) can tell which classifier
# produced a verdict. Bump when this module's routing changes observably.
COMPLETION_CLASSIFIER_VERSION = "switchboard.completion_classifier.v4"

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

#: Branch-protection projection of Switchboard's *own* merge gate, published by
#: ``scripts/switchboard_pr_gate.run_merge_authorization_for_pr``.
#:
#: It is not an independent CI signal.  It republishes the same merge_gate
#: findings the hydrator already places on ``snapshot["findings"]``, collapsed
#: into one red/green bit -- the GitHub commit-status API carries only
#: state/context/description, so the typed ``code`` cannot survive the trip.
#: Routing off the projection therefore double-counts evidence that is already
#: in hand and destroys its type on the way: a draft PR, a missing review
#: verdict, and a missing ``executed_test_run`` all arrive here as one
#: undifferentiated red context, which ``normalize_status_context`` can only
#: attribute to ``product`` -- stamping every process-state problem as
#: ``required_exact_head_ci_failed`` and burning a remediation runner on work
#: with nothing to remediate (COORD-49).
MERGE_AUTHORIZATION_CONTEXT = "Switchboard / merge authorization"

# One snapshot can contain several independently blocking facts.  Their source
# order is presentation detail, not authority.  Prefer concrete automatic work
# first so it is not stranded behind a weaker wait/retry signal; retain human
# blockers ahead of evidence-production and coordination repairs.
_ROUTE_PRECEDENCE = {
    "remediation": 0,
    "human": 1,
    "review_merge": 2,
    "coordination_retry": 3,
    "wait": 4,
}


def _map(value: Any) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _text_raw(value: Any) -> str:
    """Trim only. Context names and URLs are identifiers — folding case breaks them."""
    return str(value or "").strip()


def _finding_detail(finding: Mapping[str, Any], *keys: str) -> Any:
    """Read a detail off a merge-gate finding, whatever shape it arrived in.

    ``_merge_gate_finding`` SPLATS its ``details`` argument onto the finding, so a
    conflicted PR arrives as ``{"code": "pr_not_mergeable", "mergeable": False, ...}``
    and ``finding["details"]`` does not exist.

    Every consumer in this module goes through here because reading it by hand went
    wrong twice in one day, in two unrelated fixes, by two authors — COORD-61's artifact
    lift and BUG-182's conflict decomposer both read ``finding.get("details")``, both
    returned nothing for every real finding, and both shipped green because their tests
    hand-built the nested shape they assumed. One accessor that knows the truth is worth
    more than two correct call sites.

    The nested form is still honoured: preflight findings (``work_sessions.py``) really
    do nest under ``details``, and a caller may hand us either family.
    """
    nested = finding.get("details")
    nested = nested if isinstance(nested, Mapping) else {}
    for key in keys:
        if key in finding and finding.get(key) is not None:
            return finding.get(key)
        if key in nested and nested.get(key) is not None:
            return nested.get(key)
    return None


def _head(record: Mapping[str, Any]) -> str:
    nested = record.get("head")
    if isinstance(nested, Mapping):
        return str(nested.get("sha") or "").strip()
    return str(record.get("head_sha") or record.get("sha") or "").strip()


def _pr_url(record: Mapping[str, Any]) -> str:
    nested = _map(record.get("verdict"))
    return str(
        record.get("html_url")
        or record.get("url")
        or record.get("pr_url")
        or nested.get("html_url")
        or nested.get("pr_url")
        or nested.get("url")
        or ""
    ).strip()


def _pr_number(record: Mapping[str, Any]) -> int:
    nested = _map(record.get("verdict"))
    raw = (
        record.get("number")
        or record.get("pr_number")
        or nested.get("number")
        or nested.get("pr_number")
    )
    try:
        number = int(raw or 0)
    except (TypeError, ValueError):
        number = 0
    if number > 0:
        return number
    match = re.search(r"/pull/(\d+)(?:/|$)", _pr_url(record))
    return int(match.group(1)) if match else 0


def _pr_identity(record: Mapping[str, Any]) -> str:
    url = _pr_url(record).rstrip("/").lower()
    match = re.search(
        r"^https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)$", url,
    )
    if match:
        return "/".join(match.groups())
    api_match = re.search(
        r"^https?://api\.github\.com/repos/([^/]+)/([^/]+)/pulls/(\d+)$",
        url,
    )
    if api_match:
        owner, repo, number = api_match.groups()
        return f"github.com/{owner}/{repo}/{number}"
    return url or (f"number:{_pr_number(record)}" if _pr_number(record) else "")


def _review_matches_pr(
    review: Mapping[str, Any], *, head_sha: str, pr_identity: str,
) -> bool:
    return bool(
        head_sha
        and pr_identity
        and str(review.get("head_sha") or _map(review.get("verdict")).get(
            "head_sha") or "").strip() == head_sha
        and _pr_identity(review) == pr_identity
    )


def _check_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        if any(k in value for k in ("name", "context", "state", "status", "conclusion")):
            return [_map(value)]
        rows = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                row = _map(item)
                row.setdefault("name", str(key))
                rows.append(row)
            else:
                rows.append({"name": str(key), "state": item})
        return rows
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rows: list[dict[str, Any]] = []
        for item in value:
            rows.extend(_check_rows(item))
        return rows
    return []


_CHECK_TIMESTAMP_FIELDS = (
    "completedAt", "completed_at",
    "updatedAt", "updated_at",
    "startedAt", "started_at",
    "createdAt", "created_at",
    "timestamp",
)
_CHECK_IDENTITY_FIELDS = {"name", "context", "check_name"}


def _check_timestamp(row: Mapping[str, Any]) -> float:
    """Return the newest valid timestamp carried by one CI observation."""
    timestamps: list[float] = []
    for field in _CHECK_TIMESTAMP_FIELDS:
        value = row.get(field)
        if value in (None, "") or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            parsed = float(value)
        else:
            text = str(value).strip()
            try:
                parsed = float(text)
            except ValueError:
                try:
                    parsed_dt = datetime.fromisoformat(
                        text[:-1] + "+00:00" if text.endswith("Z") else text
                    )
                    if parsed_dt.tzinfo is None:
                        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                    parsed = parsed_dt.timestamp()
                except (ValueError, OverflowError):
                    continue
        if math.isfinite(parsed):
            timestamps.append(parsed)
    return max(timestamps, default=float("-inf"))


def _has_provenance(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    return True


def _check_provenance_richness(row: Mapping[str, Any]) -> int:
    """Count non-empty evidence fields, excluding interchangeable name fields."""
    return sum(
        1 for key, value in row.items()
        if key not in _CHECK_IDENTITY_FIELDS and _has_provenance(value)
    )


def _check_selection_key(row: Mapping[str, Any]) -> tuple[float, int, str]:
    """Total order for duplicate CI observations, independent of source order."""
    canonical = json.dumps(
        dict(row),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return (
        _check_timestamp(row),
        _check_provenance_richness(row),
        canonical,
    )


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
            current = contexts.get(name)
            if name and (
                current is None
                or _check_selection_key(row) > _check_selection_key(current)
            ):
                contexts[name] = row
    required = list(dict.fromkeys(
        str(item).strip() for item in
        [*(gate.get("required_status_contexts") or []), *required_status_contexts]
        if str(item).strip()
    ))
    board_git = _map(task_row.get("git_state"))
    board_pr = {
        "number": board_git.get("pr_number"),
        "url": board_git.get("pr_url"),
    }
    current_pr_url = (
        _pr_url(pr) or str(gate.get("pr_url") or "").strip()
        or str(board_git.get("pr_url") or "").strip()
    )
    snapshot = {
        "schema": COMPLETION_SNAPSHOT_SCHEMA,
        "task": task_row,
        "task_id": str(task_row.get("task_id") or gate.get("task_id") or "").strip().upper(),
        "board_status": str(task_row.get("status") or "").strip(),
        "board_head_sha": _head(board_git),
        "board_pr_number": board_git.get("pr_number"),
        "board_pr_url": board_git.get("pr_url"),
        "board_pr_identity": _pr_identity(board_pr),
        "github_pr": pr,
        "pr_number": pr.get("number") or gate.get("pr_number"),
        "pr_url": current_pr_url,
        "pr_identity": _pr_identity({
            "number": pr.get("number") or gate.get("pr_number"),
            "url": current_pr_url,
        }),
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


def _select_decision(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Select one blocker without allowing source ordering to choose it."""
    if not candidates:
        return None
    return dict(min(
        candidates,
        key=lambda item: (
            _ROUTE_PRECEDENCE.get(_text(item.get("route")), 99),
            _text(item.get("reason_code")),
            _text(item.get("desired_role")),
        ),
    ))


#: Merge-gate finding codes whose refusal is about a *missing artifact* rather than a
#: failed check. Their gate result carries a ``missing_artifact`` report naming the
#: expected key, schema, accepted hash keys, read surfaces and any near-miss.
_ARTIFACT_GATE_DETAIL_KEYS = ("executed_test_gate", "ui_playwright_gate")


def _missing_artifact_identity(
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Lift the ``missing_artifact`` report off an evidence-family gate finding.

    COORD-61, extending COORD-51 §3.3 from the CI family to the evidence family. The
    merge gate computed this at the moment it refused and it already rides on the
    snapshot's findings; the classifier was dropping it, so a repair runner received a
    bare ``missing_executed_test_run`` and re-derived nothing.

    Measured on CO-21, 2026-07-25: a runner had executed five real suites and written
    them under ``executed_tests``. The correct evidence was one key away in
    worksession-aa0ccd80bb504bbd, and the attempt-2 repair dispatch — handed only the
    reason code — wrote nothing, orphaned a fresh work session, and exited.

    Read-only and diagnostic: it contributes no candidate and no precedence input.

    Reads through :func:`_finding_detail` because ``_merge_gate_finding`` splats its
    ``details``: there is no ``finding["details"]`` to read. The first cut of this
    function looked under that key and returned ``{}`` for every real finding — the
    report was written by the gate and dropped one layer later, the exact defect it was
    written to remove.
    """
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        if finding.get("blocking") is False:
            continue
        for key in _ARTIFACT_GATE_DETAIL_KEYS:
            gate = _map(_finding_detail(finding, key))
            report = _map(gate.get("missing_artifact"))
            if report:
                return {"missing_artifact": report}
    return {}


def _finding_decision(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Map merge_gate codes; never infer a route from aggregate PR findings."""
    candidates: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
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
            candidates.append(
                _decision("blocked", "human", code or failure, board="Blocked")
            )
        elif code in _REVIEW_FINDINGS:
            candidates.append(
                _decision("assessing", "review_merge", code, role="review_merge")
            )
        elif (
            code in _REMEDIATION_FINDINGS
            or kind in {"automatic", "product", "code"}
            or failure == "hidden_fallback"
        ):
            candidates.append(_decision(
                "blocked", "remediation", code or failure,
                role="remediation", board="Blocked",
            ))
        elif kind in {"judgment", "authority", "policy", "human"}:
            candidates.append(
                _decision("blocked", "human", code or kind, board="Blocked")
            )
        elif code in _COORD_FINDINGS or failure in {
            "broken_connection", "missing_data", "stale_branch", "unreachable_agent",
        }:
            candidates.append(_decision(
                "blocked", "coordination_retry", code or failure,
                retry="bounded",
            ))
        elif failure == "failed_gate":
            candidates.append(_decision(
                "blocked", "human", code or "unclassified_failed_gate",
                board="Blocked",
            ))
    selected = _select_decision(candidates)
    # Attached AFTER selection, exactly as `_required_ci_decision` attaches the failing
    # check identity: `_select_decision` keys only on route, reason_code and
    # desired_role, so this cannot influence which blocker wins. Feature-only.
    if selected is not None:
        selected.update(_missing_artifact_identity(findings))
    return selected


def _is_merge_authorization_context(name: Any) -> bool:
    return _text(name) == _text(MERGE_AUTHORIZATION_CONTEXT)


def _typed_findings_present(findings: Any) -> bool:
    """Report whether the merge gate's own typed originals are on the snapshot.

    This is the fail-closed guard on deferring the merge-authorization
    projection.  Deferring is only honest when the evidence the projection was
    derived from is actually in hand and still carries a route-bearing
    identifier; if hydrate failed or returned nothing, skipping the red context
    would drop the signal entirely and let a genuinely blocked PR read clean.
    """
    if isinstance(findings, Mapping) or isinstance(findings, (str, bytes)):
        return False
    if not isinstance(findings, Sequence):
        return False
    return any(
        isinstance(finding, Mapping)
        and finding.get("blocking") is not False
        and (
            _text(finding.get("code"))
            or _text(finding.get("failure_class"))
            or _text(finding.get("finding_class") or finding.get("kind"))
        )
        for finding in findings
    )


_FAILED_CI_STATES = {"failure", "failed", "error", "timed_out", "action_required"}


def _failing_check_identity(
    failing: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Name the required contexts that failed, from the rows already in hand.

    Outcomes spec §3.3. ``_required_ci_decision`` iterated these rows to reach its
    verdict and then dropped their identity, so ``required_exact_head_ci_failed``
    reported that CI failed and never which check — seven CO-20 remediation runners
    each received that signal, and the failing suite was identified only by cloning
    the head and running it by hand.

    Diagnostic only: it contributes no candidate, no route, and no precedence input.

    Ordered by context name, never by the order the provider happened to present the
    contexts in. COORD-49 was a source-ordering bug that chose a decision, and
    `_select_decision` exists so ordering cannot; a diagnostic that varied under
    permutation would reintroduce exactly that non-determinism into the classifier's
    output — the 57,624-permutation model test in tests/test_bug172_* asserts the whole
    decision is permutation-invariant, and it is right to.
    """
    if not failing:
        return {}
    ordered = sorted(
        ((name, _map(row)) for name, row in failing if name),
        key=lambda item: item[0],
    )
    if not ordered:
        return {}
    identity: dict[str, Any] = {
        "failing_contexts": [name for name, _ in ordered],
    }
    # One representative check, chosen by the same total order.
    _, representative = ordered[0]
    url = _text_raw(
        representative.get("target_url") or representative.get("url")
        or representative.get("details_url") or representative.get("detailsUrl")
        or representative.get("run_url")
    )
    if url:
        identity["failing_check_url"] = url
    summary = _text_raw(
        representative.get("description") or representative.get("summary")
        or representative.get("output_title")
    )
    if summary:
        identity["failing_check_summary"] = summary
    return identity


def _required_ci_decision(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """Classify every required context, then select by explicit precedence."""
    required = list(snapshot.get("required_status_contexts") or [])
    contexts = _map(snapshot.get("status_contexts"))
    # The merge-authorization projection is a restatement of these findings, not
    # a second observation. Defer to the typed originals and the branches that
    # own them (review, findings, mergeability, draft) whenever they are present.
    defer_merge_authorization = _typed_findings_present(snapshot.get("findings"))
    candidates: list[dict[str, Any]] = []
    failing: list[tuple[str, Mapping[str, Any]]] = []
    for name in required:
        if defer_merge_authorization and _is_merge_authorization_context(name):
            continue
        row = _map(contexts.get(name))
        state = _text(row.get("conclusion") or row.get("state") or row.get("status"))
        attribution = _text(
            row.get("failure_attribution") or row.get("failure_class") or "unknown"
        )
        if not row or not state:
            reason = (
                "required_ci_missing"
                if snapshot.get("ci_dispatch_recent")
                else "required_ci_hydration_missing"
            )
            route = "wait" if snapshot.get("ci_dispatch_recent") else "coordination_retry"
            candidates.append(_decision(
                "waiting" if route == "wait" else "blocked",
                route,
                reason,
                retry="bounded",
            ))
        elif state in _PENDING:
            candidates.append(_decision(
                "waiting", "wait", "required_exact_head_ci_pending",
                retry="bounded",
            ))
        elif state in _COORD_CI:
            candidates.append(_decision(
                "blocked", "coordination_retry",
                f"required_ci_{state}", retry="bounded",
            ))
        elif state in _FAILED_CI_STATES:
            failing.append((_text_raw(name), row))
            if state == "action_required" or attribution in {"policy", "authority"}:
                candidates.append(_decision(
                    "blocked", "human", "required_ci_authority_failure",
                    board="Blocked",
                ))
            elif attribution == "product":
                candidates.append(_decision(
                    "blocked", "remediation", "required_exact_head_ci_failed",
                    role="remediation", board="Blocked",
                ))
            elif attribution == "infrastructure" or state == "timed_out":
                candidates.append(_decision(
                    "blocked", "coordination_retry",
                    "required_ci_infrastructure_failure", retry="bounded",
                ))
            else:
                candidates.append(_decision(
                    "blocked", "human", "required_ci_failure_unknown",
                    board="Blocked",
                ))
        elif state not in _POLICY_PASS:
            candidates.append(_decision(
                "blocked", "coordination_retry", "required_ci_state_unknown",
                retry="bounded",
            ))
    selected = _select_decision(candidates)
    # Attach the retained identity AFTER selection: `_select_decision` keys only on
    # route, reason_code and desired_role, so these fields cannot influence which
    # blocker wins. §3.3 is feature-only — if a route changes because of this, that
    # is a defect, not a spec change.
    if selected is not None:
        selected.update(_failing_check_identity(failing))
    return selected


def _changes_requested_decision(
    review: Mapping[str, Any],
    head_sha: str,
    pr_identity: str,
) -> dict[str, Any] | None:
    """Classify findings only when the verdict is for this exact PR and head."""
    review_state = _text(
        review.get("status") or review.get("state") or review.get("verdict")
    )
    if review_state not in {"changes_requested", "changes"}:
        return None
    if not _review_matches_pr(
        review, head_sha=head_sha, pr_identity=pr_identity
    ):
        return None
    if review.get("retry_exhausted"):
        return _decision(
            "blocked", "human", "review_retry_budget_exhausted", board="Blocked",
        )

    findings = [_map(item) for item in (review.get("findings") or [])]
    automatic = [
        item for item in findings
        if _text(item.get("finding_class") or item.get("kind") or item.get("class"))
        in {"automatic", "product", "code", "auto"}
    ]
    escalated = [item for item in findings if item not in automatic]
    if automatic:
        # A current-head repair contract is actionable even when a CI adapter
        # has not hydrated its contexts yet. The next remediation generation
        # can repair these findings while coordination separately heals CI.
        decision = _decision(
            "blocked", "remediation", "automatic_review_findings",
            role="remediation", board="Blocked",
        )
        decision["acceptance_findings"] = automatic
        decision["escalated_findings"] = escalated
        return decision

    decision = _decision(
        "blocked", "human", "human_review_findings", board="Blocked",
    )
    decision["escalated_findings"] = escalated
    return decision


def _merge_conflict_decision(
    pr: Mapping[str, Any],
    findings: Sequence[Any],
) -> dict[str, Any] | None:
    """Detect an agent-fixable merge conflict from the PR or gate findings.

    ``pr_not_mergeable`` findings are aggregate and are skipped by
    ``_finding_decision`` so this module can decompose them. When live
    ``github_pr`` hydration is empty, the finding's ``details`` are the only
    remaining conflict evidence — use them rather than pretending the PR is
    missing (BUG-182).
    """
    mergeable = pr.get("mergeable")
    merge_state = _text(
        pr.get("mergeStateStatus") or pr.get("mergeable_state") or pr.get("merge_state")
    )
    if mergeable is False or merge_state in {"dirty", "conflicting"}:
        return _decision(
            "blocked", "remediation", "pr_merge_conflict",
            role="remediation", board="Blocked",
        )
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        if _text(finding.get("code")) != "pr_not_mergeable":
            continue
        # BUG-182's decomposer read finding["details"] and therefore never fired: the
        # gate splats details, so the conflict evidence sits on the finding itself.
        detail_mergeable = _finding_detail(finding, "mergeable")
        detail_state = _text(_finding_detail(
            finding, "merge_state", "mergeStateStatus", "mergeable_state"))
        if detail_mergeable is False or detail_state in {"dirty", "conflicting"}:
            return _decision(
                "blocked", "remediation", "pr_merge_conflict",
                role="remediation", board="Blocked",
            )
    return None


def _board_named_pr(snap: Mapping[str, Any], board_pr_identity: str) -> bool:
    return bool(
        board_pr_identity
        or snap.get("board_pr_number")
        or str(snap.get("board_pr_url") or "").strip()
    )


def _github_pr_hydration_unavailable(
    findings: Sequence[Any],
) -> dict[str, Any]:
    """Board still names a PR, but live hydration did not produce one."""
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        if _text(finding.get("code")) == "github_pr_state_unavailable":
            return _decision(
                "blocked", "coordination_retry", "github_pr_state_unavailable",
                retry="bounded",
            )
    return _decision(
        "blocked", "coordination_retry", "github_pr_state_unavailable",
        retry="bounded",
    )


def _classify_completion_base(
    current_run: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one deterministic, side-effect-free decision for an exact-head snapshot."""
    del current_run  # the convergence ladder in classify_completion reads it; base never does
    snap = _map(snapshot)
    task_status = _text(snap.get("board_status") or _map(snap.get("task")).get("status"))
    provenance = _map(snap.get("merge_provenance"))
    pr = _map(snap.get("github_pr"))
    queue = _map(snap.get("merge_queue"))
    review = _map(snap.get("review"))
    runner = _map(snap.get("runner"))
    findings = list(snap.get("findings") or [])
    head_sha = str(snap.get("head_sha") or _head(pr)).strip()
    board_head = str(snap.get("board_head_sha") or "").strip()
    current_pr_identity = str(
        snap.get("pr_identity")
        or _pr_identity({
            "number": snap.get("pr_number") or pr.get("number"),
            "url": snap.get("pr_url") or _pr_url(pr),
        })
    ).strip()
    board_pr_identity = str(
        snap.get("board_pr_identity")
        or _pr_identity({
            "number": snap.get("board_pr_number"),
            "url": snap.get("board_pr_url"),
        })
    ).strip()
    conflict = _merge_conflict_decision(pr, findings)

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
        # A board-named PR (or a DIRTY finding already on the snapshot) is never
        # "exact head has no PR". Reserve exact_head_pr_missing for the genuine
        # empty case so conflicted/DIRTY work reaches remediation (BUG-182).
        if conflict:
            return conflict
        if _board_named_pr(snap, board_pr_identity):
            return _github_pr_hydration_unavailable(findings)
        return _decision("blocked", "coordination_retry", "exact_head_pr_missing",
                         retry="bounded")
    if not current_pr_identity:
        return _decision(
            "blocked", "coordination_retry", "exact_pr_identity_missing",
            retry="bounded",
        )
    if board_pr_identity and board_pr_identity != current_pr_identity:
        return _decision(
            "blocked", "coordination_retry", "board_pr_identity_mismatch",
            retry="bounded",
        )
    if board_head and board_head != head_sha:
        return _decision("blocked", "coordination_retry", "board_pr_head_mismatch",
                         retry="bounded")

    # Conflicts are ordinary, agent-fixable rebase work. They outrank queue /
    # CI / review / evidence findings so DIRTY PRs dispatch remediation instead
    # of spinning on bounded infra retry (BUG-182).
    if conflict:
        return conflict

    queue_state = _text(queue.get("state") or queue.get("status"))
    if queue_state in {"merged", "complete", "completed"}:
        return _decision("reconciling", "reconcile", "merge_queue_merged")

    # Exact-head changes requested are already a complete, actionable repair
    # contract. They outrank nonterminal merge-queue and CI coordination state,
    # but never canonical evidence that the queue has already merged the PR.
    review_decision = _changes_requested_decision(
        review, head_sha, current_pr_identity)
    if review_decision:
        return review_decision

    queue_failure = _text(queue.get("failure_attribution") or queue.get("failure_class"))
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

    ci_decision = _required_ci_decision(snap)
    # Product / authority CI failures still outrank draft (code is wrong or policy
    # blocked). Transient CI wait/hydration must not hide BREAKDOWN 5: undrafting
    # is free and must surface as draft_ready_to_mark_ready while CI is pending.
    if ci_decision and ci_decision.get("route") in {"remediation", "human"}:
        return ci_decision

    if pr.get("draft") is True:
        return _decision("ready_to_queue", "review_merge", "draft_ready_to_mark_ready",
                         role="review_merge", effect="mark_ready_then_reread")

    if ci_decision:
        return ci_decision

    review_state = _text(
        review.get("status") or review.get("state") or review.get("verdict")
    )
    if review.get("retry_exhausted"):
        return _decision("blocked", "human", "review_retry_budget_exhausted", board="Blocked")
    review_passed = review_state in {"pass", "passed", "approved", "success"}
    if (
        not review_passed
        or not _review_matches_pr(
            review, head_sha=head_sha, pr_identity=current_pr_identity)
    ):
        return _decision("assessing", "review_merge",
                         "review_verdict_stale" if review_passed else "review_required",
                         role="review_merge")

    finding_route = _finding_decision(findings)
    if finding_route:
        return finding_route

    mergeable = pr.get("mergeable")
    merge_state = _text(
        pr.get("mergeStateStatus") or pr.get("mergeable_state") or pr.get("merge_state")
    )
    if merge_state == "behind":
        return _decision("blocked", "coordination_retry", "pr_branch_behind",
                         retry="bounded")
    if merge_state == "unknown" or mergeable is None:
        route = "coordination_retry" if snap.get("mergeability_retry_exhausted") else "wait"
        return _decision("blocked" if route != "wait" else "waiting", route,
                         "pr_mergeability_unknown", retry="bounded")
    # BLOCKED and UNSTABLE have now been decomposed through exact-head CI,
    # review, findings, and conflicts. They are never decisions by themselves.

    desired_role = None
    runner_role = _text(runner.get("role") or runner.get("execution_role"))
    runner_head = str(runner.get("head_sha") or "").strip()
    if runner and runner.get("live") and (runner_role != desired_role or runner_head != head_sha):
        # A clean snapshot needs no role runner; stale live execution must be fenced
        # before queueing, but remains an orchestration repair rather than coding work.
        return _decision("blocked", "coordination_retry", "live_runner_not_desired",
                         retry="bounded", effect="fence_runner")

    # Tip gates are green and there is no live queue entry. Distinguish
    # "never queued" (enqueue once) from "GitHub ejected after a verified
    # enqueue" (requeue). Treating eject as a fresh enqueue is a no-op under
    # ONCE_ONLY / idempotent replay — that is the BREAKDOWN 38 / 40 jam.
    removal = _text(queue.get("last_removal_reason") or queue.get("removal_reason"))
    if (
        not queue_state
        and (
            queue.get("prior_enqueue_verified") is True
            or removal in {"failed_checks", "checks_timed_out"}
        )
    ):
        return _decision(
            "blocked", "coordination_retry", "merge_queue_ejected_tip_green",
            retry="bounded", effect="requeue_merge_group",
        )

    return _decision("ready_to_queue", "review_merge", "exact_head_gates_passed",
                     role="review_merge", effect="enqueue")


#: Combined convergence pressure (decision churn + stable no-op replays at one
#: head) after which a coordination_retry stops repeating itself. COORD-57
#: (2026-07-26) reached decision_attempt=50; three identical outcomes is enough
#: to know the current strategy is not converging.
CONVERGENCE_LADDER_PRESSURE = 3


def _convergence_ladder(
    current_run: Mapping[str, Any] | None,
    snap: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Bound non-converging coordination retries with a deterministic ladder.

    COORD-77, forced by the COORD-57 incident: a coordination_retry that keeps
    producing identical outcomes at one head has two shapes the single-tick
    contract cannot see — oscillation (decision flips re-arm the same repair
    with a fresh idem_key; prod reached attempt=50) and livelock (a stable
    decision's verified effect replays as a no-op forever, the task frozen and
    unreported). ``completion_runs`` counts both per head (``churn`` +
    ``stable_replays``, reset on head movement, because a moved head IS
    progress).

    Once pressure reaches ``CONVERGENCE_LADDER_PRESSURE``, the ladder runs
    exactly two deterministic rungs, then names the failure:

      1. one forced ``reconcile`` (``completion_not_converging_reconcile``) —
         a fresh authoritative re-read, the cheapest deterministic move when
         staleness caused the churn;
      2. if pressure persists after that, a NAMED ``human`` closeout
         (``completion_not_converging``) carrying the original blocker. This is
         the last rung, not the fix: by construction there is no deterministic
         move left, and naming the stall beats spinning on it silently.

    Never fires below pressure, on any non-coordination_retry route, or across
    a head change — ordinary bounded retries and remediation loops that make
    progress are untouched.
    """
    if _text(decision.get("route")) != "coordination_retry":
        return dict(decision)
    run = _map(current_run)
    convergence = _map(_map(run.get("evidence_refs")).get("convergence"))
    head = str(_map(snap).get("head_sha") or "").strip().lower()
    if not convergence or not head:
        return dict(decision)
    if str(convergence.get("head_sha") or "").lower() != head:
        return dict(decision)
    pressure = (
        int(convergence.get("churn") or 0)
        + int(convergence.get("stable_replays") or 0)
    )
    if pressure < CONVERGENCE_LADDER_PRESSURE:
        return dict(decision)
    original_reason = str(decision.get("reason_code") or "")
    if not convergence.get("reconcile_tried"):
        escalated = _decision(
            "reconciling", "reconcile", "completion_not_converging_reconcile",
            retry="bounded", effect="reconcile_provenance")
        escalated["convergence"] = {
            "pressure": pressure, "original_reason_code": original_reason}
        return escalated
    escalated = _decision(
        "blocked", "human", "completion_not_converging", board="Blocked")
    escalated["convergence"] = {
        "pressure": pressure, "original_reason_code": original_reason}
    escalated["escalated_findings"] = [{
        "code": "completion_not_converging",
        "message": (
            f"Completion did not converge: {pressure} identical outcomes at "
            f"this head (original blocker: {original_reason or 'unknown'}); "
            "a forced reconcile did not change the outcome."
        ),
        "original_reason_code": original_reason,
        "blocking": True,
    }]
    return escalated


def classify_completion(
    current_run: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify one exact-head snapshot, bounded by the convergence ladder.

    The base classifier is pure world-in/decision-out. The ladder layers the
    retry-budget policy ``current_run`` was always reserved for: identical
    coordination_retry outcomes at one head escalate deterministically
    (reconcile once, then a named human closeout) instead of repeating forever.
    """
    decision = _classify_completion_base(current_run, snapshot)
    return _convergence_ladder(current_run, snapshot, decision)
