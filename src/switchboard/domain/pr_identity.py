"""Canonical GitHub pull-request identity without URL-shape authority."""
from __future__ import annotations

import re
from typing import Any


_PR_URL_PATTERNS = (
    re.compile(r"/repos/([^/]+/[^/]+)/pulls/(\d+)(?:/|$)", re.IGNORECASE),
    re.compile(r"/([^/]+/[^/]+)/pull/(\d+)(?:/|$)", re.IGNORECASE),
)


def canonical_pr_identity(pr_url: Any) -> str:
    """Return ``owner/repo#number`` for GitHub API or browser PR URLs.

    Unknown URL shapes remain distinct through a normalized raw-URL fallback.
    That preserves fail-closed replacement-PR fencing without treating two
    spellings of the same GitHub PR as different work.
    """
    raw = str(pr_url or "").strip()
    if not raw:
        return ""
    for pattern in _PR_URL_PATTERNS:
        match = pattern.search(raw)
        if match:
            repository, number = match.groups()
            return f"{repository.lower()}#{int(number)}"
    return "url:" + raw.lower().rstrip("/")


def same_pr_identity(left: Any, right: Any) -> bool:
    """Compare two non-empty PR references by canonical identity."""
    left_identity = canonical_pr_identity(left)
    right_identity = canonical_pr_identity(right)
    return bool(left_identity and right_identity and left_identity == right_identity)


__all__ = ["canonical_pr_identity", "same_pr_identity"]
