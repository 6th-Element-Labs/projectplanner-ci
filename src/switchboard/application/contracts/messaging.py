"""Backward-compatible re-exports of versioned messaging contracts."""
from switchboard.contracts.messaging.v1 import (
    ACK_MESSAGE_COMMAND_SCHEMA,
    SEND_AGENT_MESSAGE_COMMAND_SCHEMA,
    AckMessageCommand,
    SendAgentMessageCommand,
)

__all__ = [
    "ACK_MESSAGE_COMMAND_SCHEMA",
    "SEND_AGENT_MESSAGE_COMMAND_SCHEMA",
    "AckMessageCommand",
    "SendAgentMessageCommand",
]
