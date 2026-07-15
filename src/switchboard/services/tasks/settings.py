"""Typed settings for the Tasks cut-out process (ARCH-MS-90)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TasksServiceSettings:
    """Env-driven settings for the Tasks service unit.

    Default port ``8122`` avoids the monolith (:8110), MCP (:8111), skeleton
    (:8120), and Auth (:8121).
    """

    service_name: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "TasksServiceSettings":
        return cls(
            service_name=(
                os.environ.get("SWITCHBOARD_TASKS_SERVICE_NAME") or "switchboard-tasks"
            ).strip(),
            host=(os.environ.get("SWITCHBOARD_TASKS_HOST") or "127.0.0.1").strip(),
            port=int(os.environ.get("SWITCHBOARD_TASKS_PORT") or "8122"),
        )
