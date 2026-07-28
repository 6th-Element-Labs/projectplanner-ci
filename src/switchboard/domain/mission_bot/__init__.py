"""Mission Bot — dumb HQ that boots agents with dossiers.

Ordered facts in → one of eight outputs out. No classifier. Human involvement
only via agent-authored ``agent_requires_human`` MCP receipt.
"""
from switchboard.domain.mission_bot.adapter import (
    MissionPorts,
    execute_mission_command,
)
from switchboard.domain.mission_bot.dossier import build_dossier
from switchboard.domain.mission_bot.outputs import (
    MISSION_BOT_VERSION,
    MISSION_COMMAND_SCHEMA,
    MISSION_DOSSIER_SCHEMA,
    MissionOutput,
)
from switchboard.domain.mission_bot.reducer import reduce_mission

__all__ = [
    "MISSION_BOT_VERSION",
    "MISSION_COMMAND_SCHEMA",
    "MISSION_DOSSIER_SCHEMA",
    "MissionOutput",
    "MissionPorts",
    "build_dossier",
    "execute_mission_command",
    "reduce_mission",
]
