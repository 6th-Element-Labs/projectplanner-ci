"""Shared external-effect ledger commands for REST and MCP (ARCH-MS-67).

Adapters own auth and transport serialization. Persistence lives in
``repositories/external_effects`` (ARCH-MS-54).
"""
from __future__ import annotations

from typing import Any, Optional

from constants import DEFAULT_PROJECT
from switchboard.storage.repositories import external_effects as effects_repo


def claim_mapping_result(
        data: dict[str, Any], *, actor: str, principal_id: str = "") -> dict[str, Any]:
    """Claim (or replay) one external side effect from adapter mapping input."""
    payload = data.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return {"error": "payload must be a JSON object", "error_code": "invalid_payload"}
    return effects_repo.claim_external_effect(
        data.get("effect_type") or "",
        data.get("target") or "",
        data.get("resource") or "",
        payload,
        task_id=data.get("task_id") or data.get("task") or None,
        claim_id=data.get("claim_id") or "",
        agent_id=data.get("agent_id") or "",
        idem_key=data.get("idem_key") or "",
        idempotency_window_seconds=int(data.get("idempotency_window_seconds") or 0),
        actor=actor,
        principal_id=principal_id,
        project=data.get("project") or DEFAULT_PROJECT,
    )


def mark_issued_mapping_result(
        data: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Mark a claimed effect as issued after provider submission."""
    readback = data.get("readback") or {}
    if not isinstance(readback, dict):
        return {"error": "readback must be a JSON object", "error_code": "invalid_readback"}
    return effects_repo.mark_external_effect_issued(
        data.get("effect_key") or data.get("id") or "",
        readback=readback,
        actor=actor,
        project=data.get("project") or DEFAULT_PROJECT,
    )


def verify_mapping_result(data: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Confirm an effect after provider readback / explicit proof."""
    readback = data.get("readback") or {}
    if not isinstance(readback, dict):
        return {"error": "readback must be a JSON object", "error_code": "invalid_readback"}
    return effects_repo.verify_external_effect(
        data.get("effect_key") or data.get("id") or "",
        readback=readback,
        actor=actor,
        project=data.get("project") or DEFAULT_PROJECT,
    )


def fail_mapping_result(data: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Record a failed or dead-lettered external effect."""
    readback = data.get("readback") or {}
    if not isinstance(readback, dict):
        return {"error": "readback must be a JSON object", "error_code": "invalid_readback"}
    return effects_repo.fail_external_effect(
        data.get("effect_key") or data.get("id") or "",
        error=data.get("error") or "effect_failed",
        readback=readback,
        dead_letter=bool(data.get("dead_letter")),
        actor=actor,
        project=data.get("project") or DEFAULT_PROJECT,
    )


def list_mapping_result(
        *,
        effect_type: str = "",
        status: str = "",
        task_id: str = "",
        target: str = "",
        project: Optional[str] = None) -> list[dict[str, Any]]:
    """List external effects for read adapters."""
    return effects_repo.list_external_effects(
        effect_type=effect_type,
        status=status,
        task_id=task_id,
        target=target,
        project=project or DEFAULT_PROJECT,
    )
