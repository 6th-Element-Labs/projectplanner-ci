"""Ask Taikun session ↔ ProjectContext binding (SEG-4).

Bindings live in the switchboard control DB meta table and record the last
authorized ProjectContext for (session, principal). Board switches intentionally
rebind; request routing still always uses the explicit ``?project=`` / auth scope
so one board's Ask history cannot be read under another project's query.
"""
from __future__ import annotations

from typing import Optional

from switchboard.domain.projects.context import ProjectContext
from switchboard.storage.repositories import activity as activity_repo
from switchboard.storage.repositories import projects as projects_repo

_META_PREFIX = "ask_session_binding:"
_CONTROL_PROJECT = "switchboard"


def _key(session: str, principal_id: str) -> str:
    principal = (principal_id or "").strip() or "anonymous"
    return f"{_META_PREFIX}{(session or 'plan').strip()}:{principal}"


def _ensure_control_db() -> None:
    """Bindings live in the switchboard control DB — initialize schema on first use."""
    try:
        projects_repo.init_db(_CONTROL_PROJECT)
    except Exception:
        pass


def bind_ask_session(ctx: ProjectContext, *, session: str) -> ProjectContext:
    """Persist the Ask Taikun session binding for ``ctx`` (rebinds on board switch)."""
    _ensure_control_db()
    session_id = (session or "plan").strip() or "plan"
    key = _key(session_id, ctx.principal_id)
    activity_repo.set_meta(
        key,
        {
            "project_id": ctx.project_id,
            "source": ctx.source,
            "principal_id": ctx.principal_id,
            "session": session_id,
        },
        project=_CONTROL_PROJECT,
    )
    return ProjectContext(
        project_id=ctx.project_id,
        source="session",
        principal_id=ctx.principal_id,
        label=ctx.label,
    )


def require_ask_session_project(
    ctx: ProjectContext,
    *,
    session: str,
) -> ProjectContext:
    """Authorize Ask access under the explicit ProjectContext (rebind if needed)."""
    return bind_ask_session(ctx, session=session)


def peek_ask_session_project(session: str, principal_id: str = "") -> Optional[str]:
    _ensure_control_db()
    existing = activity_repo.get_meta(_key(session, principal_id), project=_CONTROL_PROJECT)
    if isinstance(existing, dict):
        bound = (existing.get("project_id") or "").strip()
        return bound or None
    return None
