"""Typed settings for the Auth cut-out process (ARCH-MS-75)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthServiceSettings:
    """Env-driven settings for the Auth service unit.

    Default port ``8121`` avoids the monolith (:8110), MCP (:8111), and the
    dormant skeleton (:8120).
    """

    service_name: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "AuthServiceSettings":
        return cls(
            service_name=(
                os.environ.get("SWITCHBOARD_AUTH_SERVICE_NAME") or "switchboard-auth"
            ).strip(),
            host=(os.environ.get("SWITCHBOARD_AUTH_HOST") or "127.0.0.1").strip(),
            port=int(os.environ.get("SWITCHBOARD_AUTH_PORT") or "8121"),
        )
