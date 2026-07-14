"""Wake contracts — re-export the current version."""
from .v1 import (
    CLAIM_WAKE_COMMAND_SCHEMA,
    COMPLETE_WAKE_COMMAND_SCHEMA,
    REQUEST_WAKE_COMMAND_SCHEMA,
    ClaimWakeCommand,
    CompleteWakeCommand,
    RequestWakeCommand,
    parse_object_payload,
)

__all__ = [
    "CLAIM_WAKE_COMMAND_SCHEMA",
    "COMPLETE_WAKE_COMMAND_SCHEMA",
    "REQUEST_WAKE_COMMAND_SCHEMA",
    "ClaimWakeCommand",
    "CompleteWakeCommand",
    "RequestWakeCommand",
    "parse_object_payload",
]
