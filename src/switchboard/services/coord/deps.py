"""Configurable Coord port holders; production adapters live outside this package."""
from __future__ import annotations

from typing import Optional

from .ports import CoordQueryPort, CoordReadAuthPort


_queries: Optional[CoordQueryPort] = None
_auth: Optional[CoordReadAuthPort] = None


def configure(*, queries: CoordQueryPort, auth: CoordReadAuthPort) -> None:
    global _queries, _auth
    _queries = queries
    _auth = auth


def is_configured() -> bool:
    return _queries is not None and _auth is not None


def _ensure() -> None:
    if is_configured():
        return
    from switchboard.api.coord_port_adapters import configure_coord_ports

    configure_coord_ports()


def queries() -> CoordQueryPort:
    _ensure()
    assert _queries is not None
    return _queries


def auth() -> CoordReadAuthPort:
    _ensure()
    assert _auth is not None
    return _auth
