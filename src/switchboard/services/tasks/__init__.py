"""Tasks process-cut service package (ARCH-MS-87 ports; live uvicorn later).

Holds independence Protocols + configurable deps for the future Tasks
process (ADR-0012 Mode A). Adapters that wrap root ``store`` / ``auth`` live in
``switchboard.api.tasks_port_adapters`` so this package stays free of forbidden
monolith imports. Standalone ``create_app`` / Caddy cutover are ARCH-MS-90+.
"""
from __future__ import annotations

from . import deps, ports

__all__ = ("deps", "ports")
