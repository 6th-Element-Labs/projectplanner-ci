from __future__ import annotations

from .ports import IngestAuthPort, IngestPort

_ingest: IngestPort | None = None
_auth: IngestAuthPort | None = None


def configure(*, ingest: IngestPort, auth: IngestAuthPort) -> None:
    global _ingest, _auth
    _ingest, _auth = ingest, auth


def ports() -> tuple[IngestPort, IngestAuthPort]:
    if _ingest is None or _auth is None:
        from switchboard.api.ingest_port_adapters import configure_ingest_ports
        configure_ingest_ports()
    assert _ingest is not None and _auth is not None
    return _ingest, _auth
