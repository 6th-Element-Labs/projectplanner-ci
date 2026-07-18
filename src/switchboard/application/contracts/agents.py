"""Backward-compatible re-exports of versioned agent contracts."""
from switchboard.contracts.agents.v1 import (
    BeginHostEnrollmentCommand,
    CompleteHostEnrollmentCommand,
    FinalizeHostEnrollmentCommand,
    RegisterAgentCommand,
    RegisterHostCommand,
    RevokeHostIdentityCommand,
    RotateHostIdentityCommand,
    UpdateHostExecutionPolicyCommand,
    parse_json_list,
    parse_json_object,
)

__all__ = [
    "RegisterAgentCommand",
    "RegisterHostCommand",
    "BeginHostEnrollmentCommand",
    "CompleteHostEnrollmentCommand",
    "FinalizeHostEnrollmentCommand",
    "RotateHostIdentityCommand",
    "RevokeHostIdentityCommand",
    "UpdateHostExecutionPolicyCommand",
    "parse_json_list",
    "parse_json_object",
]
