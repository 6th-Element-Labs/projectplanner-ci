"""Ports for the read-only Coord/board day-one process boundary."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from fastapi import Request


@runtime_checkable
class CoordQueryPort(Protocol):
    """Project-scoped query surface required by ADR-0013 day one."""

    def board(self, project: str, *, cards: bool = False) -> dict[str, Any]: ...

    def signals(self, project: str) -> dict[str, Any]: ...

    def delta(self, project: str, *, since_cursor: int = 0,
              lane: str = "") -> dict[str, Any]: ...

    def coordination(self, project: str, *, limit: int = 500) -> dict[str, Any]: ...

    def coordinator_decisions(
        self,
        project: str,
        *,
        task_id: str = "",
        deliverable_id: str = "",
        decision_kind: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class CoordReadAuthPort(Protocol):
    """Shared Auth boundary for every project-scoped Coord read."""

    def authorize(self, request: Request, project: str) -> dict[str, Any]: ...
