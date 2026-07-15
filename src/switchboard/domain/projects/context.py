"""Immutable project scope resolved once at ingress (SEG-4).

``ProjectContext`` is the request/job boundary object passed into shared
application commands. It is distinct from ``store.get_project_context``
(UI metadata: repo roles / hierarchy), which remains a storage query.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectContext:
    """Validated project identity for one ingress request or background job."""

    project_id: str
    source: str  # query | body | path | session | adapter:legacy_maxwell_default
    principal_id: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        if not (self.project_id or "").strip():
            raise ValueError("ProjectContext.project_id is required")
        if not (self.source or "").strip():
            raise ValueError("ProjectContext.source is required")
