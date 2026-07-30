"""Typed recognition for wake rows returned by the coordination plane."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, NotRequired, TypeGuard, TypedDict


class ControlPlaneUnavailable(TypedDict):
    """The documented fail-closed row returned when a wake read is unavailable."""

    error: Literal["control_plane_unavailable"]
    reason: str
    operation: NotRequired[str]
    elapsed_ms: NotRequired[float]


def is_control_plane_unavailable(value: object) -> TypeGuard[ControlPlaneUnavailable]:
    """Return whether *value* is the coordination read-unavailable sentinel."""

    return (
        isinstance(value, dict)
        and value.get("error") == "control_plane_unavailable"
        and not value.get("wake_id")
    )


def genuine_wake_intents(rows: Iterable[object]) -> list[Mapping[str, Any]]:
    """Keep genuine wake rows and fail closed on sentinels or malformed rows."""

    return [
        row
        for row in rows
        if (
            isinstance(row, Mapping)
            and not is_control_plane_unavailable(row)
            and bool(row.get("wake_id"))
        )
    ]


__all__ = [
    "ControlPlaneUnavailable",
    "genuine_wake_intents",
    "is_control_plane_unavailable",
]
