"""Configurable Tasks port holders — set by ``tasks_port_adapters.configure_tasks_ports``.

The Tasks service package never imports root ``store`` / ``auth`` / ``dispatch``;
adapters outside the package inject implementations here.
"""
from __future__ import annotations

from typing import Optional

from .ports import (
    ClaimLifecyclePort,
    TaskBoardPort,
    TaskPrincipalPort,
    TaskWriteBindingPort,
    WorkSessionLookupPort,
)

_principal: Optional[TaskPrincipalPort] = None
_write_binding: Optional[TaskWriteBindingPort] = None
_board: Optional[TaskBoardPort] = None
_claims: Optional[ClaimLifecyclePort] = None
_work_sessions: Optional[WorkSessionLookupPort] = None


def configure(
    *,
    principal: TaskPrincipalPort,
    write_binding: TaskWriteBindingPort,
    board: TaskBoardPort,
    claims: ClaimLifecyclePort,
    work_sessions: WorkSessionLookupPort,
) -> None:
    """Bind production (or test) adapters into the Tasks package."""
    global _principal, _write_binding, _board, _claims, _work_sessions
    _principal = principal
    _write_binding = write_binding
    _board = board
    _claims = claims
    _work_sessions = work_sessions


def is_configured() -> bool:
    return (
        _principal is not None
        and _write_binding is not None
        and _board is not None
        and _claims is not None
        and _work_sessions is not None
    )


def _ensure() -> None:
    if is_configured():
        return
    from switchboard.api.tasks_port_adapters import configure_tasks_ports

    configure_tasks_ports()


def principal() -> TaskPrincipalPort:
    _ensure()
    assert _principal is not None
    return _principal


def write_binding() -> TaskWriteBindingPort:
    _ensure()
    assert _write_binding is not None
    return _write_binding


def board() -> TaskBoardPort:
    _ensure()
    assert _board is not None
    return _board


def claims() -> ClaimLifecyclePort:
    _ensure()
    assert _claims is not None
    return _claims


def work_sessions() -> WorkSessionLookupPort:
    _ensure()
    assert _work_sessions is not None
    return _work_sessions
