"""Backward-compatible re-exports of versioned agent contracts."""
from switchboard.contracts.agents.v1 import (
    RegisterAgentCommand,
    RegisterHostCommand,
    parse_json_list,
    parse_json_object,
)

__all__ = [
    "RegisterAgentCommand",
    "RegisterHostCommand",
    "parse_json_list",
    "parse_json_object",
]
