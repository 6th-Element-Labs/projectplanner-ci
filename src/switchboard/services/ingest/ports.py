from __future__ import annotations

from typing import Any, Mapping, Protocol
from fastapi import Request


class IngestPort(Protocol):
    def list_inbox(self, project: str, status: str | None = None) -> dict[str, Any]: ...
    def intake(self, project: str, body: Mapping[str, Any], idem_key: str) -> dict[str, Any]: ...


class IngestAuthPort(Protocol):
    def authorize(self, request: Request, project: str, scopes: tuple[str, ...]) -> dict[str, Any]: ...
