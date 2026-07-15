"""Resolve explicit project scope into ProjectContext (SEG-4)."""
from __future__ import annotations

from typing import Optional

from switchboard.domain.projects.context import ProjectContext
from switchboard.storage.repositories import access as access_repo


class MissingProjectScope(ValueError):
    """Raised when customer ingress omits project identity."""


class UnknownProjectScope(ValueError):
    """Raised when the requested project id is not in the registry."""


class ConflictingProjectScope(ValueError):
    """Raised when two explicit project sources disagree."""


def require_explicit_project(
    raw: Optional[str],
    *,
    source: str,
    principal_id: str = "",
    label: str = "",
    validate_registry: bool = True,
) -> ProjectContext:
    """Fail closed on empty/whitespace project; optionally reject unknown ids."""
    project_id = (raw or "").strip()
    if not project_id:
        raise MissingProjectScope("project required")
    if validate_registry and not access_repo.has_project(project_id):
        raise UnknownProjectScope(f"unknown project: {project_id}")
    return ProjectContext(
        project_id=project_id,
        source=source,
        principal_id=principal_id,
        label=label,
    )


def reconcile_explicit_projects(
    *candidates: tuple[Optional[str], str],
    principal_id: str = "",
) -> ProjectContext:
    """Pick one explicit project; conflict if two non-empty values disagree."""
    chosen: Optional[tuple[str, str]] = None
    for raw, source in candidates:
        value = (raw or "").strip()
        if not value:
            continue
        if chosen is None:
            chosen = (value, source)
            continue
        if chosen[0] != value:
            raise ConflictingProjectScope(
                f"conflicting project scope: {chosen[0]!r} ({chosen[1]}) vs {value!r} ({source})"
            )
    if chosen is None:
        raise MissingProjectScope("project required")
    return require_explicit_project(chosen[0], source=chosen[1], principal_id=principal_id)
