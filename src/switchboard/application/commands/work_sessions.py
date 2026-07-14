"""Work-session application commands.

REST and MCP adapters both call these helpers for create / managed-create /
update / preflight / archive. Authentication and response serialization stay at
their edges. Persistence remains on ``store`` /
:mod:`switchboard.storage.repositories.work_sessions`.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import store

CreateWorkSessionFn = Callable[..., dict[str, Any]]
UpdateWorkSessionFn = Callable[..., dict[str, Any]]
PreflightWorkSessionFn = Callable[..., dict[str, Any]]
ArchiveWorkSessionFn = Callable[..., dict[str, Any]]


def create(
        payload: dict[str, Any],
        *,
        actor: str,
        principal_id: str = "",
        project: str,
        create_fn: Optional[CreateWorkSessionFn] = None) -> dict[str, Any]:
    """Create one Work Session through the persistence boundary."""
    creator = create_fn or store.create_work_session
    return creator(
        dict(payload or {}),
        actor=actor,
        principal_id=principal_id,
        project=project,
    )


def create_managed(
        payload: dict[str, Any],
        *,
        actor: str,
        principal_id: str = "",
        project: str,
        create_fn: Optional[CreateWorkSessionFn] = None) -> dict[str, Any]:
    """Create a managed worktree/clone Work Session."""
    creator = create_fn or store.create_managed_work_session
    return creator(
        dict(payload or {}),
        actor=actor,
        principal_id=principal_id,
        project=project,
    )


def update(
        work_session_id: str,
        payload: dict[str, Any],
        *,
        actor: str,
        project: str,
        update_fn: Optional[UpdateWorkSessionFn] = None) -> dict[str, Any]:
    """Apply a sparse Work Session update."""
    updater = update_fn or store.update_work_session
    return updater(
        work_session_id,
        dict(payload or {}),
        actor=actor,
        project=project,
    )


def preflight(
        work_session_id: str,
        *,
        actor: str,
        project: str,
        expected_branch: str = "",
        expected_base_ref: str = "",
        preflight_fn: Optional[PreflightWorkSessionFn] = None) -> dict[str, Any]:
    """Run repo preflight for a Work Session and persist hygiene."""
    runner = preflight_fn or store.preflight_work_session
    return runner(
        work_session_id,
        actor=actor,
        project=project,
        expected_branch=expected_branch,
        expected_base_ref=expected_base_ref,
    )


def archive(
        work_session_id: str,
        *,
        actor: str,
        project: str,
        remove_workspace: bool = False,
        archive_fn: Optional[ArchiveWorkSessionFn] = None) -> dict[str, Any]:
    """Archive a managed Work Session workspace."""
    archiver = archive_fn or store.archive_work_session_workspace
    return archiver(
        work_session_id,
        remove_workspace=bool(remove_workspace),
        actor=actor,
        project=project,
    )
