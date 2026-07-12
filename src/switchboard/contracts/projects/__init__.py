"""Project registry contracts."""
from .impact import (
    PROJECT_IMPACT_REPORT_SCHEMA,
    ProjectImpactReport,
)
from .v2 import (
    PROJECT_RECORD_SCHEMA,
    PROJECT_UPDATE_COMMAND_SCHEMA,
    ProjectRecord,
    ProjectUpdateCommand,
)

__all__ = [
    "PROJECT_IMPACT_REPORT_SCHEMA",
    "PROJECT_RECORD_SCHEMA",
    "PROJECT_UPDATE_COMMAND_SCHEMA",
    "ProjectImpactReport",
    "ProjectRecord",
    "ProjectUpdateCommand",
]
