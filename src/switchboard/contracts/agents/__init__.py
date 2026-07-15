"""Agent contracts — re-export the current version."""
from .v1 import (
    BEGIN_HOST_ENROLLMENT_COMMAND_SCHEMA,
    COMPLETE_HOST_ENROLLMENT_COMMAND_SCHEMA,
    REGISTER_AGENT_COMMAND_SCHEMA,
    REGISTER_HOST_COMMAND_SCHEMA,
    REVOKE_HOST_IDENTITY_COMMAND_SCHEMA,
    ROTATE_HOST_IDENTITY_COMMAND_SCHEMA,
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
    "REGISTER_AGENT_COMMAND_SCHEMA",
    "REGISTER_HOST_COMMAND_SCHEMA",
    "BEGIN_HOST_ENROLLMENT_COMMAND_SCHEMA",
    "COMPLETE_HOST_ENROLLMENT_COMMAND_SCHEMA",
    "ROTATE_HOST_IDENTITY_COMMAND_SCHEMA",
    "REVOKE_HOST_IDENTITY_COMMAND_SCHEMA",
    "BeginHostEnrollmentCommand",
    "CompleteHostEnrollmentCommand",
    "RegisterAgentCommand",
    "RegisterHostCommand",
    "RevokeHostIdentityCommand",
    "RotateHostIdentityCommand",
    "parse_json_list",
    "parse_json_object",
]
