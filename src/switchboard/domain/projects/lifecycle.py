"""Project registry lifecycle rules — fail-closed transitions and protected records."""
from __future__ import annotations

from typing import Any, Mapping

PROJECT_LIFECYCLE_STATUSES = frozenset({"active", "archived"})
PROJECT_LIFECYCLE_WRITE_BLOCK_SCHEMA = "switchboard.project_lifecycle_write_block.v1"


class ProjectLifecycleWriteBlocked(PermissionError):
    """Raised by the central store boundary when an archived project is mutated."""

    def __init__(self, project_id: str, operation: str = "write") -> None:
        self.project_id = str(project_id or "").strip()
        self.operation = str(operation or "write").strip() or "write"
        self.detail = lifecycle_write_block(self.project_id, "archived", self.operation)
        super().__init__(self.detail["message"])


def lifecycle_write_block(project_id: str, lifecycle_status: str,
                          operation: str = "write") -> dict[str, Any] | None:
    """Return the stable denial contract for an archived project, else ``None``."""
    status = normalize_lifecycle_status(lifecycle_status)
    if status != "archived":
        return None
    pid = str(project_id or "").strip()
    op = str(operation or "write").strip() or "write"
    return {
        "schema": PROJECT_LIFECYCLE_WRITE_BLOCK_SCHEMA,
        "error": "project_archived",
        "failure_class": "failed_gate",
        "project_id": pid,
        "lifecycle_status": "archived",
        "operation": op,
        "message": (
            f"project '{pid}' is archived; '{op}' is read-only until restore_project succeeds"
        ),
    }


def assert_project_write_allowed(project_id: str, lifecycle_status: str,
                                 operation: str = "write") -> None:
    """Raise the typed denial used by REST, MCP, schedulers, and store entry points."""
    if lifecycle_write_block(project_id, lifecycle_status, operation):
        raise ProjectLifecycleWriteBlocked(project_id, operation)


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
