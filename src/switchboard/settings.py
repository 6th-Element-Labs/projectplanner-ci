"""Typed settings for the Switchboard platform package (Phase 0 scaffold).

Backed by pydantic-settings (already a dependency, via ``mcp``) so environment
parsing, type coercion, and defaults are declared once as typed fields instead of
hand-written ``os.environ.get(...)`` + ``int(...)`` calls. This is the canonical
home for *startup-snapshot* configuration — values read once when a process boots
(ports, auth mode, public base URL). Per-request/dynamic knobs that are
intentionally re-read on every call (e.g. the SQLite write-queue and concurrency
limiter) deliberately stay on their own ``os.environ`` reads.

``from_env()`` is retained for existing call sites; it is just ``cls()``.
"""
from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_prefix maps each field to PM_<UPPER_FIELD> (auth_mode -> PM_AUTH_MODE,
    # mcp_port -> PM_MCP_PORT, public_base_url -> PM_PUBLIC_BASE_URL). app_port keeps
    # the historical PM_PORT name via an explicit alias (alias wins over the prefix).
    # frozen mirrors the previous immutable dataclass; extra env vars are ignored so
    # the many other PM_* knobs in the environment never trip construction.
    model_config = SettingsConfigDict(
        env_prefix="PM_", frozen=True, extra="ignore", case_sensitive=False)

    auth_mode: str = "dev-open"
    public_base_url: str = ""
    mcp_port: int = 8111
    app_port: int = Field(default=8110, validation_alias="PM_PORT")

    @field_validator("auth_mode", "public_base_url", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()
