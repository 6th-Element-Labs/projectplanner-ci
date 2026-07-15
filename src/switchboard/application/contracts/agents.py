"""Backward-compatible re-exports of versioned agent contracts."""
from switchboard.contracts.agents.v1 import (
    BeginHostEnrollmentCommand,
    CompleteHostEnrollmentCommand,
    RegisterAgentCommand,
    RegisterHostCommand,
    RevokeHostIdentityCommand,
    RotateHostIdentityCommand,
    parse_json_list,
    parse_json_object,
)

__all__ = [
    "RegisterAgentCommand",
    "RegisterHostCommand",
    "BeginHostEnrollmentCommand",
    "CompleteHostEnrollmentCommand",
    "RotateHostIdentityCommand",
    "RevokeHostIdentityCommand",
    "parse_json_list",
    "parse_json_object",
]
