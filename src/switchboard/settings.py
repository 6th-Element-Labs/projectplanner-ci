"""Typed settings for the Switchboard platform package (Phase 0 scaffold)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    auth_mode: str
    public_base_url: str
    mcp_port: int
    app_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            auth_mode=(os.environ.get("PM_AUTH_MODE") or "dev-open").strip(),
            public_base_url=(os.environ.get("PM_PUBLIC_BASE_URL") or "").strip(),
            mcp_port=int(os.environ.get("PM_MCP_PORT", "8111")),
            app_port=int(os.environ.get("PM_PORT", "8110")),
        )
