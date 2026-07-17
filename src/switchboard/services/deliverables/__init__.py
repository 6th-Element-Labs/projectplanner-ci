"""Deliverables/mission process-cut service (ARCH-MS-110).

Standalone FastAPI + uvicorn unit for the read-only ADR-0014 day-one surface on
``127.0.0.1:8124``. Repository and Auth implementations are injected through
ports from ``switchboard.api.deliverables_port_adapters``. Live Caddy cutover
remains a successor task.
"""
from __future__ import annotations

from typing import Any

from . import deps, ports
from .settings import DeliverablesServiceSettings

__all__ = ("DeliverablesServiceSettings", "create_app", "deps", "ports")


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app as _create_app

        return _create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
