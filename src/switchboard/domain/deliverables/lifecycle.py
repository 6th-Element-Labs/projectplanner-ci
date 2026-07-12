"""Deliverable and mission-board lifecycle rules."""
from __future__ import annotations

import re
import uuid
from typing import Any, Mapping

from constants import PROJECT_ID_SLUG_RE


DELIVERABLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{1,127}$")
PROJECT_BOARD_ID_RE = DELIVERABLE_ID_RE
PROJECT_BOARD_KINDS = frozenset({"board", "mission"})
PROJECT_BOARD_STATUSES = frozenset({"proposed", "active", "paused", "blocked", "done", "archived"})
DELIVERABLE_STATUSES = frozenset({
    "proposed", "approved", "in_progress", "blocked", "in_review", "done", "archived",
})
DELIVERABLE_MILESTONE_STATUSES = frozenset({
    "not_started", "in_progress", "blocked", "in_review", "done", "skipped",
})
BREAKDOWN_PROPOSAL_STATUSES = frozenset({"proposed", "approved", "rejected", "superseded", "deferred"})
DONE_CLOSURE_GRADES = frozenset({"pass", "waive"})
CLOSURE_METADATA_KEYS = frozenset({
    "closure_reports", "last_closure_report", "last_closure_grade", "last_closure_at",
})


def _slug(value: str) -> str:
    slug = PROJECT_ID_SLUG_RE.sub("-", (value or "").strip().lower()).strip("-_")
    return re.sub(r"[-_]{2,}", "-", slug)


def normalize_deliverable_id(value: str = "", title: str = "") -> str:
    raw = (value or "").strip()
    if raw:
        candidate = raw
    else:
        slug = _slug(title or "")
        candidate = f"deliverable-{slug}" if slug else f"deliverable-{uuid.uuid4().hex[:12]}"
    if not DELIVERABLE_ID_RE.match(candidate):
        raise ValueError(
            "deliverable id must be 2-128 chars and start with a letter; "
            "letters, digits, '_', '-', '.', and ':' are allowed"
        )
    return candidate


def normalize_project_board_id(value: str = "", title: str = "") -> str:
    raw = (value or "").strip()
    if raw:
        candidate = raw
    else:
        slug = _slug(title or "")
        candidate = f"mission-{slug}" if slug else f"mission-{uuid.uuid4().hex[:12]}"
    if not PROJECT_BOARD_ID_RE.match(candidate):
        raise ValueError(
            "board id must be 2-128 chars and start with a letter; "
            "letters, digits, '_', '-', '.', and ':' are allowed"
        )
    return candidate


def validate_deliverable_status(status: str) -> dict[str, Any] | None:
    if status not in DELIVERABLE_STATUSES:
        return {"error": "invalid status", "allowed": sorted(DELIVERABLE_STATUSES)}
    return None


def validate_milestone_status(status: str) -> dict[str, Any] | None:
    if status not in DELIVERABLE_MILESTONE_STATUSES:
        return {"error": "invalid milestone status",
                "allowed": sorted(DELIVERABLE_MILESTONE_STATUSES)}
    return None


def merge_deliverable_metadata(
        prior_metadata: Mapping[str, Any],
        incoming_metadata: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(incoming_metadata)
    for key in CLOSURE_METADATA_KEYS:
        if key in prior_metadata:
            merged[key] = prior_metadata[key]
        else:
            merged.pop(key, None)
    return merged


def done_requires_closure_grade(
        *,
        deliverable_id: str,
        requested_status: str,
        last_closure_grade: str | None) -> dict[str, Any] | None:
    grade = str(last_closure_grade or "").lower()
    if requested_status == "done" and grade not in DONE_CLOSURE_GRADES:
        return {
            "error": "deliverable closure grade required",
            "deliverable_id": deliverable_id,
            "requested_status": requested_status,
            "last_closure_grade": grade or None,
            "allowed_closure_grades": sorted(DONE_CLOSURE_GRADES),
            "action": "run verify_deliverable_closure and persist a pass or waive grade",
            "spec": "docs/DELIVERABLE-CLOSURE-GATE.md",
        }
    return None
