"""Typed environment settings for the Deliverables cut-out process."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliverablesServiceSettings:
    """Runtime identity and localhost binding for the Deliverables service."""

    service_name: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "DeliverablesServiceSettings":
        return cls(
            service_name=(
                os.environ.get("SWITCHBOARD_DELIVERABLES_SERVICE_NAME")
                or "switchboard-deliverables"
            ).strip(),
            host=(
                os.environ.get("SWITCHBOARD_DELIVERABLES_HOST") or "127.0.0.1"
            ).strip(),
            port=int(os.environ.get("SWITCHBOARD_DELIVERABLES_PORT") or "8124"),
        )
