"""Agent contracts — re-export the current version."""
from .v1 import (
    REGISTER_AGENT_COMMAND_SCHEMA,
    REGISTER_HOST_COMMAND_SCHEMA,
    RegisterAgentCommand,
    RegisterHostCommand,
    parse_json_list,
    parse_json_object,
)

__all__ = [
    "REGISTER_AGENT_COMMAND_SCHEMA",
    "REGISTER_HOST_COMMAND_SCHEMA",
    "RegisterAgentCommand",
    "RegisterHostCommand",
    "parse_json_list",
    "parse_json_object",
]
