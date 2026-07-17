"""Configurable Deliverables ports; production adapters live outside this package."""
from __future__ import annotations

from typing import Optional

from .ports import DeliverablesQueryPort, DeliverablesReadAuthPort


_queries: Optional[DeliverablesQueryPort] = None
_auth: Optional[DeliverablesReadAuthPort] = None


def configure(*, queries: DeliverablesQueryPort, auth: DeliverablesReadAuthPort) -> None:
    global _queries, _auth
    _queries = queries
    _auth = auth


def is_configured() -> bool:
    return _queries is not None and _auth is not None


def _ensure() -> None:
    if is_configured():
        return
    from switchboard.api.deliverables_port_adapters import configure_deliverables_ports

    configure_deliverables_ports()


def queries() -> DeliverablesQueryPort:
    _ensure()
    assert _queries is not None
    return _queries


def auth() -> DeliverablesReadAuthPort:
    _ensure()
    assert _auth is not None
    return _auth
