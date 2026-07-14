"""Shared create-deliverable command for REST and MCP (ARCH-MS-65).

Adapters own auth and transport serialization. Persistence stays behind
``store.create_deliverable`` (repositories/deliverables).
"""
from __future__ import annotations

from typing import Any

import store


def execute_mapping_result(
        data: dict[str, Any], *, actor: str, project: str) -> dict[str, Any]:
    """Create or upsert one deliverable from adapter mapping input."""
    return store.create_deliverable(data or {}, actor=actor, project=project)
