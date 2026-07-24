"""Immutable project scope and authorization context (SEG-3/SEG-4).

The same boundary object is intentionally shared by MCP, REST, jobs, and
application commands.  Scope-only callers populate the first four fields;
authorized callers also carry the effective scopes and audited grants resolved
once at ingress. It is distinct from ``store.get_project_context`` (UI
metadata: repo roles / hierarchy), which remains a storage query.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ProjectGrant:
    project_id: str
    subject_kind: str
    subject_id: str
    role: str
    scopes: tuple[str, ...]
    created_at: float
    created_by: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProjectGrant":
        return cls(
            project_id=str(value.get("project_id") or ""),
            subject_kind=str(value.get("subject_kind") or ""),
            subject_id=str(value.get("subject_id") or ""),
            role=str(value.get("role") or ""),
            scopes=tuple(sorted(str(scope) for scope in (value.get("scopes") or []))),
            created_at=float(value.get("created_at") or 0.0),
            created_by=str(value.get("created_by") or ""),
        )


@dataclass(frozen=True)
class ProjectContext:
    """Validated project identity plus optional effective authorization."""

    project_id: str
    source: str  # query | body | path | session | adapter:legacy_maxwell_default
    principal_id: str = ""
    label: str = ""
    requested_project: str = ""
    principal_kind: str = ""
    principal_binding: str = ""
    principal_display_name: str = ""
    access_class: str = ""
    effective_scopes: tuple[str, ...] = ()
    grants: tuple[ProjectGrant, ...] = ()
    authorized_projects: tuple[str, ...] = ()
    environment_operator: bool = False
    dev_open: bool = False
    bound_task_id: str = ""
    bound_agent_id: str = ""
    bound_host_id: str = ""
    bound_wake_id: str = ""
    bound_runner_session_id: str = ""

    def __post_init__(self) -> None:
        if not (self.project_id or "").strip():
            raise ValueError("ProjectContext.project_id is required")
        if not (self.source or "").strip():
            raise ValueError("ProjectContext.source is required")

    @property
    def project(self) -> str:
        """Compatibility alias while raw ``project`` strings are retired."""
        return self.project_id

    def as_principal(self) -> dict[str, Any]:
        """Compatibility projection for existing application command adapters."""
        return {
            "id": self.principal_id,
            "kind": self.principal_kind,
            "display_name": self.principal_display_name,
            "project": self.principal_binding,
            "scopes": list(self.effective_scopes),
            "effective_scopes": list(self.effective_scopes),
            "project_roles": [
                {
                    "project_id": grant.project_id,
                    "subject_kind": grant.subject_kind,
                    "subject_id": grant.subject_id,
                    "role": grant.role,
                    "scopes": list(grant.scopes),
                    "created_at": grant.created_at,
                    "created_by": grant.created_by,
                }
                for grant in self.grants
            ],
            "environment_operator": self.environment_operator,
            "dev_open": self.dev_open,
            "bound_task_id": self.bound_task_id,
            "bound_agent_id": self.bound_agent_id,
            "bound_host_id": self.bound_host_id,
            "bound_wake_id": self.bound_wake_id,
            "bound_runner_session_id": self.bound_runner_session_id,
        }
