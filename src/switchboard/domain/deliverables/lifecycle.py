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



