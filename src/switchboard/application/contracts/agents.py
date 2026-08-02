"""Backward-compatible re-exports of versioned agent contracts."""
from switchboard.contracts.agents.v1 import (
    BeginHostEnrollmentCommand,
    CompleteHostEnrollmentCommand,
    DirectAssignmentMCPTokenCommand,
    FinalizeHostEnrollmentCommand,
    GrantHostProjectAccessCommand,
    RegisterAgentCommand,
    RegisterHostCommand,
    RevokeHostIdentityCommand,
    RevokeHostProjectAccessCommand,
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
    "DirectAssignmentMCPTokenCommand",
    "FinalizeHostEnrollmentCommand",
    "GrantHostProjectAccessCommand",
    "RotateHostIdentityCommand",
    "RevokeHostIdentityCommand",
    "RevokeHostProjectAccessCommand",
    "UpdateHostExecutionPolicyCommand",
    "parse_json_list",
    "parse_json_object",
]
