"""Provider-credential vault MCP tools (CO-6)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

from switchboard.application.commands import provider_credentials as commands
from switchboard.domain.provider_credentials import list_provider_auth_capabilities as capability_matrix
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    default_provider_credential_repository,
)


@dataclass(frozen=True)
class ProviderCredentialToolServices:
    dumps: Callable[[Any], str]
    require_read: Callable[..., dict[str, Any]]
    require_write: Callable[..., dict[str, Any]]
    principal_actor: Callable[[dict[str, Any]], str]


_SERVICES: ProviderCredentialToolServices | None = None


def _services() -> ProviderCredentialToolServices:
    if _SERVICES is None:
        raise RuntimeError("provider credential MCP tools are not registered")
    return _SERVICES


def _object(value: str, field: str) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{field} must decode to an object")
    return result


def _access(principal: dict[str, Any]) -> dict[str, Any]:
    scopes = set(principal.get("effective_scopes") or principal.get("scopes") or [])
    return {
        "principal_id": str(principal.get("id") or ""),
        "principal_kind": str(principal.get("kind") or "").lower(),
        "scopes": sorted(scopes),
        "admin": "admin" in scopes,
    }


def enroll_provider_connection(connection_json: str, ctx: Context,
                               project: str = "maxwell") -> str:
    """Enroll an encrypted personal provider identity; the response is metadata-only."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    access = _access(principal)
    principal_id, is_admin = access["principal_id"], access["admin"]
    payload = {**_object(connection_json, "connection_json"), "project": project}
    return services.dumps(commands.enroll_mapping(
        payload, actor=services.principal_actor(principal),
        principal_user_id=principal_id, admin=is_admin))


def get_provider_connection(credential_reference: str, ctx: Context,
                            project: str = "maxwell") -> str:
    """Read metadata and audit provenance without returning credential material."""
    services = _services()
    principal = services.require_read(ctx, project, ("read:credentials",))
    access = _access(principal)
    principal_id, is_admin = access["principal_id"], access["admin"]
    try:
        result = default_provider_credential_repository.get_metadata(
            credential_reference, project=project, principal_user_id=principal_id,
            admin=is_admin, include_events=True)
    except CredentialVaultError as exc:
        result = exc.as_dict()
    return services.dumps(result)


def list_provider_connections(ctx: Context, project: str = "maxwell",
                              user_id: str = "") -> str:
    """List metadata-only provider identities visible to the caller."""
    services = _services()
    principal = services.require_read(ctx, project, ("read:credentials",))
    access = _access(principal)
    principal_id, is_admin = access["principal_id"], access["admin"]
    try:
        result = {"connections": default_provider_credential_repository.list_metadata(
            project=project, principal_user_id=principal_id, admin=is_admin,
            user_id=user_id if is_admin else principal_id)}
    except CredentialVaultError as exc:
        result = exc.as_dict()
    return services.dumps(result)


def list_provider_auth_capabilities(ctx: Context, project: str = "maxwell") -> str:
    """Read the server-authoritative provider/auth/host policy matrix (CO-15)."""
    services = _services()
    services.require_read(ctx, project, ("read",))
    return services.dumps(capability_matrix())


def rotate_provider_connection(credential_reference: str, rotation_json: str,
                               ctx: Context, project: str = "maxwell") -> str:
    """Rotate an enrolled auth capsule and fence every lease on the prior version."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    access = _access(principal)
    principal_id, is_admin = access["principal_id"], access["admin"]
    payload = {**_object(rotation_json, "rotation_json"), "project": project,
               "credential_reference": credential_reference}
    return services.dumps(commands.rotate_mapping(
        payload, actor=services.principal_actor(principal),
        principal_user_id=principal_id, admin=is_admin))


def revoke_provider_connection(credential_reference: str, reason: str,
                               ctx: Context, project: str = "maxwell") -> str:
    """Revoke and cryptographically erase a provider credential, fencing active leases."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    access = _access(principal)
    principal_id, is_admin = access["principal_id"], access["admin"]
    return services.dumps(commands.revoke_mapping(
        {"project": project, "credential_reference": credential_reference, "reason": reason},
        actor=services.principal_actor(principal), principal_user_id=principal_id,
        admin=is_admin))


def delete_provider_connection(credential_reference: str, reason: str,
                               ctx: Context, project: str = "maxwell") -> str:
    """Cryptographically erase and tombstone a provider connection."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:credentials",))
    access = _access(principal)
    principal_id, is_admin = access["principal_id"], access["admin"]
    return services.dumps(commands.delete_mapping(
        {"project": project, "credential_reference": credential_reference, "reason": reason},
        actor=services.principal_actor(principal), principal_user_id=principal_id,
        admin=is_admin))


def acquire_provider_credential_lease(binding_json: str, ctx: Context,
                                      project: str = "maxwell") -> str:
    """Bind one reference to one user/account/project/task/host/runner/work-session."""
    services = _services()
    principal = services.require_write(ctx, project, ("use:credentials",))
    access = _access(principal)
    payload = {**_object(binding_json, "binding_json"), "project": project}
    return services.dumps(commands.acquire_lease_mapping(
        payload, actor=services.principal_actor(principal),
        principal=access))


def release_provider_credential_lease(lease_id: str, reason: str, ctx: Context,
                                      project: str = "maxwell") -> str:
    """Release a credential lease before runner drain, replacement, or termination."""
    services = _services()
    principal = services.require_write(ctx, project, ("use:credentials",))
    access = _access(principal)
    return services.dumps(commands.release_lease_mapping(
        {"project": project, "lease_id": lease_id, "reason": reason},
        actor=services.principal_actor(principal), principal=access))


PROVIDER_CREDENTIAL_TOOL_NAMES = (
    "enroll_provider_connection",
    "get_provider_connection",
    "list_provider_connections",
    "list_provider_auth_capabilities",
    "rotate_provider_connection",
    "revoke_provider_connection",
    "delete_provider_connection",
    "acquire_provider_credential_lease",
    "release_provider_credential_lease",
)


def register_provider_credential_tools(
        mcp: Any, services: ProviderCredentialToolServices) -> dict[str, Callable[..., str]]:
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in PROVIDER_CREDENTIAL_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
