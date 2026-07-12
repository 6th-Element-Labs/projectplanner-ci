"""Board domain — task lifecycle and dependency graph semantics."""
from .tasks import (
    DONE_STATUS_CONTRADICTION_RE,
    EDITABLE_TASK_FIELDS,
    READY_TASK_STATUSES,
    STALE_DEPENDENCY_RATIONALE_RE,
    TERMINAL_TASK_STATUSES,
    apply_terminal_done_view,
    block_done_without_provenance,
    build_dependency_state,
    is_terminal_done_task,
    normalize_depends_on,
    rationale_state,
)

__all__ = [
    "DONE_STATUS_CONTRADICTION_RE",
    "EDITABLE_TASK_FIELDS",
    "READY_TASK_STATUSES",
    "STALE_DEPENDENCY_RATIONALE_RE",
    "TERMINAL_TASK_STATUSES",
    "apply_terminal_done_view",
    "block_done_without_provenance",
    "build_dependency_state",
    "is_terminal_done_task",
    "normalize_depends_on",
    "rationale_state",
]
