"""Typed settings for a cut-out service process (ARCH-MS-73)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SkeletonSettings:
    """Env-driven settings for a skeleton (or cloned) service unit.

    Defaults intentionally use an unused localhost port so a local run cannot
    collide with the monolith (:8110) or MCP (:8111).
    """

    service_name: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "SkeletonSettings":
        return cls(
            service_name=(
                os.environ.get("SWITCHBOARD_SKELETON_SERVICE_NAME") or "switchboard-skeleton"
            ).strip(),
            host=(os.environ.get("SWITCHBOARD_SKELETON_HOST") or "127.0.0.1").strip(),
            port=int(os.environ.get("SWITCHBOARD_SKELETON_PORT") or "8120"),
        )
