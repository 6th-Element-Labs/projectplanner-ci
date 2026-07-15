"""Auth process-cut service (ARCH-MS-75).

Standalone FastAPI + uvicorn unit that reuses ``switchboard.api.routers.auth``
and binds ports via ``auth_port_adapters``. Runs side-by-side with the monolith
on ``127.0.0.1:8121``. Live Caddy ``/api/auth*`` cutover is ARCH-MS-76 — keep
production traffic on the monolith until then.
"""
from __future__ import annotations

from .app import create_app
from .settings import AuthServiceSettings

__all__ = ("AuthServiceSettings", "create_app")
