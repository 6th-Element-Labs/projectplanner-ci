"""Read-only Mission Bot shadow against a hydrated snapshot.

Shadow never mutates. It records what the Mission Bot would do so canaries and
conformance can validate before cutover.
"""
from __future__ import annotations

from typing import Any, Mapping

from switchboard.domain.mission_bot import reduce_mission
from switchboard.domain.mission_bot.outputs import MISSION_BOT_VERSION


def shadow_mission(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the Mission Bot command for ``snapshot`` without side effects."""
    command = reduce_mission(snapshot)
    return {
        "schema": "switchboard.mission_bot.shadow.v1",
        "mission_bot_version": MISSION_BOT_VERSION,
        "command": command,
        "output": command.get("output"),
        "reason_code": command.get("reason_code"),
        "role": command.get("role"),
        "dossier": command.get("dossier"),
        "mutates": False,
    }


__all__ = ["shadow_mission"]
