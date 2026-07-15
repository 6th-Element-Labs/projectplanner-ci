"""Tasks process-cut service (ARCH-MS-87 ports; ARCH-MS-90 standalone uvicorn).

Standalone FastAPI + uvicorn unit that reuses ``switchboard.api.routers.tasks``
(Mode A thin surface) and ``claims``, binding ports via
``tasks_port_adapters``. Runs side-by-side with the monolith on
``127.0.0.1:8122``. Live Caddy cutover is ARCH-MS-92 — keep production traffic
on the monolith until then.

Adapters that wrap root ``store`` / ``auth`` live in
``switchboard.api.tasks_port_adapters`` so this package stays free of forbidden
monolith imports. Fail-closed write-binding lives in ``binding`` (ARCH-MS-88).

``create_app`` is loaded lazily so ``tasks_port_adapters`` → ``services.tasks.deps``
does not circular-import the composition root at package import time.
"""
from __future__ import annotations

from typing import Any

from . import binding, deps, ports
from .settings import TasksServiceSettings

__all__ = ("TasksServiceSettings", "binding", "create_app", "deps", "ports")


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app as _create_app
        return _create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
