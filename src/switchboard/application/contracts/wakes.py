"""Backward-compatible re-exports of versioned wake contracts."""
from switchboard.contracts.wakes.v1 import (
    ClaimWakeCommand,
    CompleteWakeCommand,
    RequestWakeCommand,
    parse_object_payload,
)

__all__ = [
    "ClaimWakeCommand",
    "CompleteWakeCommand",
    "RequestWakeCommand",
    "parse_object_payload",
]
