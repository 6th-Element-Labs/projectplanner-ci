"""Project registry domain rules."""
from .context import ProjectContext
from .lifecycle import (
    PROJECT_LIFECYCLE_STATUSES,
    PROJECT_LIFECYCLE_WRITE_BLOCK_SCHEMA,
    ProjectLifecycleWriteBlocked,
    assert_project_write_allowed,
    assert_lifecycle_mutation_allowed,
    default_lifecycle_status,
    lifecycle_write_block,
    normalize_lifecycle_status,
    validate_lifecycle_transition,
)

__all__ = [
    "ProjectContext",
    "PROJECT_LIFECYCLE_STATUSES",
    "PROJECT_LIFECYCLE_WRITE_BLOCK_SCHEMA",
    "ProjectLifecycleWriteBlocked",
    "assert_project_write_allowed",
    "assert_lifecycle_mutation_allowed",
    "default_lifecycle_status",
    "lifecycle_write_block",
    "normalize_lifecycle_status",
    "validate_lifecycle_transition",
]
