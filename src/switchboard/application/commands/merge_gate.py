"""Shared merge_gate command for REST and MCP (ARCH-MS-67).

Thin adapter-facing wrapper until ARCH-MS-61 relocates merge_gate policy out of
``repositories/shell``. Authentication stays at the edge; this command does not
merge and cannot mark Done.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import store

MergeGateFn = Callable[..., dict[str, Any]]


def execute_mapping_result(
        data: dict[str, Any],
        *,
        actor: str,
        principal_id: str = "",
        merge_gate: Optional[MergeGateFn] = None) -> dict[str, Any]:
    """Evaluate safe-merge readiness from adapter mapping input."""
    payload = dict(data or {})
    project = payload.pop("project", None) or store.DEFAULT_PROJECT
    gate = merge_gate or store.merge_gate
    return gate(
        payload,
        actor=actor,
        principal_id=principal_id,
        project=project,
    )
