"""Pure authority check for cross-task review-repair completion.

Storage hydrates the records.  This module decides whether those records form
one exact, internally consistent proof.  Keeping the decision pure lets the
same invariants drive production and a large generated state-space test.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


CROSS_TASK_REPAIR_SCHEMA = "switchboard.cross_task_review_repair.v1"
MERGE_GATE_SCHEMA = "switchboard.merge_gate.v1"
RESOLVED_REMEDIATION_STATUSES = frozenset({"resolved", "resolved_with_followup"})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _ids(values: Any) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return sorted({
        _text(value)
        for value in values
        if _text(value)
    })


def _criteria_ids(criteria: Any) -> list[str]:
    if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes)):
        return []
    return sorted({
        _text(item.get("id"))
        for item in criteria
        if isinstance(item, Mapping) and _text(item.get("id"))
    })


def _blocked(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "blocked", "reason": reason, **details}


def _waiting(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "waiting", "reason": reason, **details}


def classify_cross_task_repair_proof(
    *,
    link: Mapping[str, Any] | None,
    bug_report: Mapping[str, Any] | None,
    remediation: Mapping[str, Any] | None,
    source_verdict: Mapping[str, Any] | None,
    source_findings: Sequence[Mapping[str, Any]] = (),
    repair_task: Mapping[str, Any] | None,
    repair_git: Mapping[str, Any] | None,
    repair_verdict: Mapping[str, Any] | None,
    merge_gate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify one fully hydrated proof without I/O or mutation.

    ``ready`` is the only result that authorizes first-time mutation.
    ``resolved`` is a validated monotonic replay of a previously stored receipt.
    Every missing, contradictory, stale, or partial authority fails closed.
    """
    link_row = _mapping(link)
    report = _mapping(bug_report)
    remediation_row = _mapping(remediation)
    source_review = _mapping(source_verdict)
    repair = _mapping(repair_task)
    git = _mapping(repair_git)
    review = _mapping(repair_verdict)
    gate = _mapping(merge_gate)

    if _text(link_row.get("schema")) != CROSS_TASK_REPAIR_SCHEMA:
        return _blocked("repair_link_schema_invalid")
    link_status = _text(link_row.get("status")).lower()
    if link_status not in {"linked", "resolved"}:
        return _blocked(
            "repair_link_not_active",
            repair_link_status=link_status or None,
        )

    repair_task_id = _upper(link_row.get("repair_task_id"))
    source_task_id = _upper(link_row.get("source_task_id"))
    source_verdict_id = _text(link_row.get("source_verdict_id"))
    remediation_id = _text(link_row.get("remediation_id"))
    link_finding_ids = _ids(link_row.get("finding_ids"))
    missing = sorted(
        name for name, value in {
            "repair_task_id": repair_task_id,
            "source_task_id": source_task_id,
            "source_verdict_id": source_verdict_id,
            "remediation_id": remediation_id,
            "finding_ids": link_finding_ids,
        }.items() if not value
    )
    if missing:
        return _blocked("repair_link_incomplete", missing=missing)
    if _upper(repair.get("task_id")) != repair_task_id:
        return _blocked("repair_task_mismatch")
    if _upper(report.get("source_task")) != source_task_id:
        return _blocked("bug_source_task_mismatch")

    if not remediation_row:
        return _blocked("source_remediation_not_found")
    if (
        _upper(remediation_row.get("task_id")) != source_task_id
        or _text(remediation_row.get("verdict_id")) != source_verdict_id
        or _text(remediation_row.get("remediation_id")) != remediation_id
    ):
        return _blocked("source_remediation_mismatch")
    if not source_review:
        return _blocked("source_verdict_not_found")
    if (
        _upper(source_review.get("task_id")) != source_task_id
        or _text(source_review.get("verdict_id")) != source_verdict_id
        or _text(source_review.get("head_sha"))
        != _text(remediation_row.get("source_head_sha"))
        or _text(source_review.get("pr_url"))
        != _text(remediation_row.get("source_pr_url"))
    ):
        return _blocked("source_verdict_mismatch")

    finding_rows = [_mapping(item) for item in source_findings]
    if not finding_rows:
        return _blocked("source_findings_mismatch")
    if any(
        _text(item.get("verdict_id")) != source_verdict_id
        or _upper(item.get("task_id")) != source_task_id
        or not _text(item.get("finding_id"))
        for item in finding_rows
    ):
        return _blocked("source_findings_mismatch")
    canonical_auto_ids = sorted(
        _text(item.get("finding_id"))
        for item in finding_rows
        if _text(item.get("finding_class")).lower() == "auto"
    )
    if not canonical_auto_ids or len(canonical_auto_ids) != len(set(canonical_auto_ids)):
        return _blocked("source_findings_mismatch")
    canonical_escalation_ids = sorted(
        _text(item.get("finding_id"))
        for item in finding_rows
        if _text(item.get("finding_class")).lower() != "auto"
    )
    remediation_escalation_ids = _criteria_ids(
        remediation_row.get("escalation_findings"))
    remediation_ids = _criteria_ids(remediation_row.get("acceptance_criteria"))
    if (
        link_finding_ids != canonical_auto_ids
        or remediation_ids != canonical_auto_ids
        or int(remediation_row.get("auto_finding_count") or 0)
        != len(canonical_auto_ids)
    ):
        return _blocked(
            "repair_finding_set_mismatch",
            canonical_finding_ids=canonical_auto_ids,
            remediation_finding_ids=remediation_ids,
            supplied_finding_ids=link_finding_ids,
        )
    human_followup_required = bool(canonical_escalation_ids)
    if (
        remediation_escalation_ids != canonical_escalation_ids
        or int(remediation_row.get("escalate_finding_count") or 0)
        != len(canonical_escalation_ids)
        or bool(remediation_row.get("human_intervention_required"))
        != human_followup_required
    ):
        return _blocked("source_escalation_contract_mismatch")

    if link_status == "resolved":
        receipt_head = _text(link_row.get("repair_head_sha"))
        receipt_pr_url = _text(link_row.get("repair_pr_url"))
        receipt_pr_number = link_row.get("repair_pr_number")
        receipt_merge = _text(link_row.get("repair_merged_sha"))
        receipt_verdict = _text(link_row.get("repair_verdict_id"))
        has_pr_identity = bool(
            receipt_pr_url and receipt_pr_number not in (None, "", 0)
        )
        legacy_pr_identity_confirmed = bool(
            not has_pr_identity
            and _text(repair.get("status")) == "Done"
            and bool(git.get("in_main_content"))
            and _text(git.get("head_sha")) == receipt_head
            and _text(git.get("merged_sha")) == receipt_merge
            and _text(git.get("pr_url"))
            and git.get("pr_number") not in (None, "", 0)
            and git.get("merged_at") not in (None, "")
        )
        if (
            not receipt_head
            or (not has_pr_identity and not legacy_pr_identity_confirmed)
            or not receipt_merge
            or not receipt_verdict
            or _text(repair.get("status")) != "Done"
            or _text(remediation_row.get("status"))
            not in RESOLVED_REMEDIATION_STATUSES
            or _text(remediation_row.get("resolved_head_sha")) != receipt_head
            or any(
                _text(item.get("finding_class")).lower() == "auto"
                and (
                    _text(item.get("state")).lower() != "fixed"
                    or _text(item.get("resolved_sha")) != receipt_head
                )
                for item in finding_rows
            )
        ):
            return _blocked("resolved_repair_receipt_invalid")
        return {
            "status": "resolved",
            "reason": "validated_resolution_receipt",
            "idempotent_replay": True,
        }

    repair_head = _text(git.get("head_sha"))
    repair_pr_url = _text(git.get("pr_url"))
    repair_pr_number = git.get("pr_number")
    merged_sha = _text(git.get("merged_sha"))
    canonical_merge = (
        _text(repair.get("status")) == "Done"
        and bool(git.get("in_main_content"))
        and bool(repair_head)
        and bool(repair_pr_url)
        and repair_pr_number not in (None, "", 0)
        and bool(merged_sha)
        and git.get("merged_at") not in (None, "")
    )
    if not canonical_merge:
        return _waiting("canonical_repair_merge_required")

    if _text(remediation_row.get("status")) not in {
        "queued", "wake_requested", "remediating", "review_pending",
        "blocked", "escalated", "wake_failed",
    }:
        return _blocked(
            "source_remediation_not_resolvable",
            source_remediation_status=remediation_row.get("status"),
        )
    if any(
        _text(item.get("finding_class")).lower() == "auto"
        and _text(item.get("state")).lower() != "open"
        for item in finding_rows
    ):
        return _blocked("source_findings_not_open")

    if (
        not review
        or _upper(review.get("task_id")) != repair_task_id
        or _text(review.get("head_sha")) != repair_head
        or _text(review.get("pr_url")) != repair_pr_url
        or _text(review.get("status")).lower() != "pass"
        or not _text(review.get("verdict_id"))
        or not _text(review.get("reviewer_principal_id"))
    ):
        return _waiting(
            "exact_pr_head_pass_required",
            repair_head_sha=repair_head,
            repair_pr_url=repair_pr_url,
        )
    if int(review.get("open_finding_count") or 0) > 0:
        return _blocked(
            "repair_verdict_has_open_findings",
            open_finding_count=int(review.get("open_finding_count") or 0),
        )
    if (
        bool(remediation_row.get("requires_adversarial_review"))
        and _text(review.get("review_mode")).lower() != "adversarial"
    ):
        return _blocked("adversarial_review_required")

    blocking_gate_findings = [
        item for item in (gate.get("findings") or [])
        if isinstance(item, Mapping) and item.get("blocking", True)
    ]
    if (
        _text(gate.get("schema")) != MERGE_GATE_SCHEMA
        or _upper(gate.get("task_id")) != repair_task_id
        or _text(gate.get("head_sha")) != repair_head
        or _text(gate.get("pr_url")) != repair_pr_url
        or str(gate.get("pr_number") or "") != str(repair_pr_number)
        or gate.get("ok") is not True
        or _text(gate.get("status")).lower() != "passed"
        or blocking_gate_findings
    ):
        return _waiting(
            "exact_pr_head_merge_gate_pass_required",
            repair_head_sha=repair_head,
            repair_pr_url=repair_pr_url,
        )

    return {
        "status": "ready",
        "reason": "exact_cross_task_repair_proof",
        "canonical_finding_ids": canonical_auto_ids,
        "human_followup_required": human_followup_required,
        "escalation_finding_ids": canonical_escalation_ids,
        "repair_head_sha": repair_head,
        "repair_pr_url": repair_pr_url,
        "repair_pr_number": repair_pr_number,
        "repair_merged_sha": merged_sha,
        "repair_verdict_id": _text(review.get("verdict_id")),
    }


__all__ = [
    "CROSS_TASK_REPAIR_SCHEMA",
    "MERGE_GATE_SCHEMA",
    "RESOLVED_REMEDIATION_STATUSES",
    "classify_cross_task_repair_proof",
]
