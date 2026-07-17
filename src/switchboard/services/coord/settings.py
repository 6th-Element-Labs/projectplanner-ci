"""Typed environment settings for the Coord cut-out process."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CoordServiceSettings:
    """Runtime identity and localhost binding for the Coord service."""

    service_name: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "CoordServiceSettings":
        return cls(
            service_name=(
                os.environ.get("SWITCHBOARD_COORD_SERVICE_NAME") or "switchboard-coord"
            ).strip(),
            host=(os.environ.get("SWITCHBOARD_COORD_HOST") or "127.0.0.1").strip(),
            port=int(os.environ.get("SWITCHBOARD_COORD_PORT") or "8123"),
        )
