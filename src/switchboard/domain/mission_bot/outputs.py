"""Mission Bot outputs — the only decisions the bot may emit.

The Mission Bot is the voice on the tape: it packages facts into a dossier and
boots an agent, or waits, or observes merge, or surfaces an agent-authored
human requirement. It does not classify, diagnose, or invent human involvement.
"""
from __future__ import annotations

from enum import Enum


class MissionOutput(str, Enum):
    START_IMPLEMENTATION = "START_IMPLEMENTATION"
    START_REMEDIATION = "START_REMEDIATION"
    START_REVIEW = "START_REVIEW"
    MARK_READY = "MARK_READY"
    ARM_MERGE = "ARM_MERGE"
    WAIT = "WAIT"
    AGENT_REQUIRES_HUMAN = "AGENT_REQUIRES_HUMAN"
    OBSERVE_MERGED = "OBSERVE_MERGED"


MISSION_BOT_VERSION = "switchboard.mission_bot.v1"
MISSION_COMMAND_SCHEMA = "switchboard.mission_command.v1"
MISSION_DOSSIER_SCHEMA = "switchboard.mission_dossier.v1"

__all__ = [
    "MISSION_BOT_VERSION",
    "MISSION_COMMAND_SCHEMA",
    "MISSION_DOSSIER_SCHEMA",
    "MissionOutput",
]
