"""Write-actor binding rules for shared env tokens and explicit system actors."""
from __future__ import annotations

from typing import Any, Mapping


UNBOUND_IDENTITY_EXPECTED_SIGNAL = (
    "The runtime identity is registered, bound, and visible to operators."
)


def is_unbound_system_actor(actor: str) -> bool:
    actor = (actor or "").strip()
    return actor in {"env-mcp-token", "env-auth-token"} or (
        actor.startswith("env-") and actor.endswith("-token")
    )


def shared_token_binding_error(
        *,
        actor: str,
        principal_id: str = "",
        task_id: str = "",
        error: str = "shared_token_requires_bound_actor",
        message: str | None = None,
        **extra: Any,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "failure_class": "unbound_identity",
        "expected_signal": UNBOUND_IDENTITY_EXPECTED_SIGNAL,
        "principal_actor": actor,
        "principal_id": principal_id,
        "task_id": task_id or None,
        "message": message,
        "remediation": [
            "Pass agent_id for a live registered agent before mutating task state.",
            "Or pass system_actor plus system_reason for deliberate automation/system writes.",
            "Register/heartbeat the runtime first if this is agent work.",
        ],
        **extra,
    }


def binding_for_principal(actor: str, *, principal_id: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "actor": (actor or "").strip() or "unknown",
        "binding": "principal",
        "principal_id": principal_id,
    }


def validate_system_actor_fields(
        system_actor: str,
        system_reason: str,
        *,
        principal_actor: str = "",
        principal_id: str = "",
        task_id: str = "",
) -> dict[str, Any] | None:
    system_actor = (system_actor or "").strip()
    system_reason = (system_reason or "").strip()
    if is_unbound_system_actor(system_actor):
        return shared_token_binding_error(
            actor=principal_actor or system_actor,
            principal_id=principal_id,
            task_id=task_id,
            error="system_actor_must_be_explicit",
            message="system_actor must name the automation, not the shared env token.",
        )
    if not system_reason:
        return shared_token_binding_error(
            actor=principal_actor or system_actor,
            principal_id=principal_id,
            task_id=task_id,
            error="system_reason_required",
            message="system_actor writes through a shared token require system_reason.",
        )
    return None


def binding_for_system_actor(
        *,
        principal_actor: str,
        principal_id: str,
        system_actor: str,
        system_reason: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "actor": system_actor.strip(),
        "binding": "explicit_system_actor",
        "principal_actor": principal_actor,
        "principal_id": principal_id,
        "system_reason": system_reason.strip(),
    }


def binding_for_registered_agent(
        *,
        agent_id: str,
        principal_actor: str,
        principal_id: str,
        binding: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "actor": agent_id.strip(),
        "binding": binding,
        "principal_actor": principal_actor,
        "principal_id": principal_id,
        "agent_id": agent_id.strip(),
    }


def write_binding_activity_payload(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "binding": binding.get("binding"),
        "actor": binding.get("actor"),
        "principal_actor": binding.get("principal_actor"),
        "principal_id": binding.get("principal_id"),
        "agent_id": binding.get("agent_id"),
        "system_reason": binding.get("system_reason"),
    }
