"""Project registry lifecycle rules — fail-closed transitions and protected records."""
from __future__ import annotations

from typing import Any, Mapping

PROJECT_LIFECYCLE_STATUSES = frozenset({"active", "archived"})


def default_lifecycle_status() -> str:
    return "active"


def normalize_lifecycle_status(value: Any) -> str:
    status = str(value or default_lifecycle_status()).strip().lower()
    return status if status in PROJECT_LIFECYCLE_STATUSES else ""


def validate_lifecycle_transition(
        current: str,
        requested: str,
        *,
        is_protected: bool = False) -> dict[str, Any] | None:
    """Return an error dict when ``requested`` is not an allowed transition."""
    cur = normalize_lifecycle_status(current) or default_lifecycle_status()
    nxt = normalize_lifecycle_status(requested)
    if not nxt:
        return {
            "error": "invalid lifecycle_status",
            "allowed": sorted(PROJECT_LIFECYCLE_STATUSES),
            "requested": requested,
        }
    if cur == nxt:
        return None
    if is_protected and nxt == "archived":
        return {
            "error": "protected project cannot be archived",
            "lifecycle_status": cur,
            "requested": nxt,
        }
    allowed = {("active", "archived"), ("archived", "active")}
    if (cur, nxt) not in allowed:
        return {
            "error": "invalid lifecycle transition",
            "from": cur,
            "to": nxt,
            "allowed": sorted(f"{a}->{b}" for a, b in allowed),
        }
    return None


def assert_lifecycle_mutation_allowed(record: Mapping[str, Any],
                                      requested: str) -> dict[str, Any] | None:
    """Validate a lifecycle transition against a registry record projection."""
    return validate_lifecycle_transition(
        str(record.get("lifecycle_status") or default_lifecycle_status()),
        requested,
        is_protected=bool(record.get("is_protected")),
    )
