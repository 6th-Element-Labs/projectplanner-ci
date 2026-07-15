"""FastAPI helpers that require explicit ProjectContext at ingress (SEG-4)."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Query, Request

from switchboard.application.commands.ask_session_binding import (
    bind_ask_session,
    require_ask_session_project,
)
from switchboard.application.queries.project_scope import (
    ConflictingProjectScope,
    MissingProjectScope,
    UnknownProjectScope,
    reconcile_explicit_projects,
    require_explicit_project,
)
from switchboard.domain.projects.context import ProjectContext


def _http_from_scope_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MissingProjectScope):
        return HTTPException(400, str(exc))
    if isinstance(exc, UnknownProjectScope):
        return HTTPException(400, str(exc))
    if isinstance(exc, ConflictingProjectScope):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


def project_query_param(project: str = Query(...)) -> str:
    """Required ``?project=`` for customer-facing FastAPI routes."""
    try:
        return require_explicit_project(project, source="query").project_id
    except (MissingProjectScope, UnknownProjectScope) as exc:
        raise _http_from_scope_error(exc) from exc


def resolve_required_project(raw: Optional[str], *, source: str = "query") -> str:
    try:
        return require_explicit_project(raw, source=source).project_id
    except (MissingProjectScope, UnknownProjectScope) as exc:
        raise _http_from_scope_error(exc) from exc


def resolve_body_project_context(body: Optional[dict]) -> ProjectContext:
    try:
        return require_explicit_project((body or {}).get("project"), source="body")
    except (MissingProjectScope, UnknownProjectScope) as exc:
        raise _http_from_scope_error(exc) from exc


def resolve_request_project_context(
    request: Request,
    *,
    body_project: Optional[str] = None,
    query_project: Optional[str] = None,
    path_project: Optional[str] = None,
) -> ProjectContext:
    principal = getattr(request.state, "principal", None) or {}
    principal_id = str(principal.get("id") or "")
    query = query_project if query_project is not None else request.query_params.get("project")
    try:
        return reconcile_explicit_projects(
            (path_project, "path"),
            (query, "query"),
            (body_project, "body"),
            principal_id=principal_id,
        )
    except (MissingProjectScope, UnknownProjectScope, ConflictingProjectScope) as exc:
        raise _http_from_scope_error(exc) from exc


def bind_ask_taikun_context(
    request: Request,
    *,
    project: str,
    session: str,
) -> ProjectContext:
    principal = getattr(request.state, "principal", None) or {}
    principal_id = str(principal.get("id") or "")
    try:
        ctx = require_explicit_project(
            project, source="query", principal_id=principal_id
        )
        return require_ask_session_project(ctx, session=session)
    except (MissingProjectScope, UnknownProjectScope, ConflictingProjectScope) as exc:
        raise _http_from_scope_error(exc) from exc


def create_ask_taikun_context(
    request: Request,
    *,
    project: str,
    session: str,
) -> ProjectContext:
    principal = getattr(request.state, "principal", None) or {}
    principal_id = str(principal.get("id") or "")
    try:
        ctx = require_explicit_project(
            project, source="query", principal_id=principal_id
        )
        return bind_ask_session(ctx, session=session)
    except (MissingProjectScope, UnknownProjectScope, ConflictingProjectScope) as exc:
        raise _http_from_scope_error(exc) from exc
