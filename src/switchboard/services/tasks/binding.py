"""Fail-closed write-binding for the Tasks package (ARCH-MS-88).

Callers resolve principals through ``TaskWriteBindingPort`` only — never by
importing root ``auth`` / ``store``. Unbound shared-token actors are denied.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import deps


class WriteBindingError(Exception):
    """Raised when write-binding fails closed (unbound identity or port error)."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        message = (
            str(self.payload.get("message") or self.payload.get("error") or "")
            or "write binding denied"
        )
        super().__init__(message)


def _is_denied(binding: Mapping[str, Any]) -> bool:
    if binding.get("ok") is False:
        return True
    if binding.get("error") or binding.get("failure_class"):
        return True
    if not (binding.get("actor") or "").strip():
        return True
    return False


def require_write_binding(
    actor: str,
    *,
    project: str = "",
    task_id: str = "",
    agent_id: str = "",
    system_actor: str = "",
    system_reason: str = "",
    principal_id: str = "",
) -> dict[str, Any]:
    """Resolve a write binding via Tasks ports; fail closed on unbound identity.

    Returns a successful binding dict (``ok`` truthy, ``actor`` set). Raises
    ``WriteBindingError`` when the port returns an unbound/error payload so
    Tasks mutations never proceed on a naked env-token principal.
    """
    binding = deps.write_binding().resolve_write_actor(
        actor,
        project=project,
        task_id=task_id,
        agent_id=agent_id,
        system_actor=system_actor,
        system_reason=system_reason,
        principal_id=principal_id,
    )
    if not isinstance(binding, Mapping) or _is_denied(binding):
        payload: dict[str, Any]
        if isinstance(binding, Mapping):
            payload = dict(binding)
        else:
            payload = {
                "ok": False,
                "error": "write_binding_port_returned_non_mapping",
                "failure_class": "unbound_identity",
                "message": "Tasks write-binding port returned an unusable payload.",
            }
        if "ok" not in payload:
            payload["ok"] = False
        if "failure_class" not in payload:
            payload["failure_class"] = "unbound_identity"
        raise WriteBindingError(payload)
    return dict(binding)


def principal_actor(principal: Mapping[str, Any]) -> str:
    """Map an Auth principal to a public write actor via ``TaskPrincipalPort``."""
    return deps.principal().actor(principal)


def activity_payload_for_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a successful binding for activity rows (port-only)."""
    return deps.write_binding().write_binding_activity_payload(binding)
