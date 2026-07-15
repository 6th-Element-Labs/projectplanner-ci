"""v1 wire DTOs for the service skeleton (ARCH-MS-73)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExamplePingResponse(BaseModel):
    """Stub response proving the contracts package boundary is importable."""

    model_config = ConfigDict(extra="forbid")

    schema_id: str = Field(
        default="switchboard.skeleton.example_ping.v1",
        alias="schema",
        description="Contract schema id for the skeleton ping response.",
    )
    ok: bool
    message: str
