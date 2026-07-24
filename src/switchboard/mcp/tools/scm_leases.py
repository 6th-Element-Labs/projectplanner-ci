"""Short-lived SCM lease MCP tools (ENFORCE-13).

A host acquires a lease after it has claimed the exact wake and registered its
runner, releases it on drain, and reads its state. Token materialization is a
trusted in-process bridge (``SCMLeaseRepository.materialize_for_runtime``) and is
deliberately NOT exposed as an MCP or REST tool — no tool here returns a token.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

from switchboard.domain.scm_leases import SCMLeasePrincipal
from switchboard.storage.repositories.scm_leases import (
    default_scm_lease_repository as repository,
)


@dataclass(frozen=True)
class SCMLeaseToolServices:
    dumps: Callable[[Any], str]
    require_read: Callable[..., dict[str, Any]]
    require_write: Callable[..., dict[str, Any]]
    principal_actor: Callable[[dict[str, Any]], str]


_SERVICES: SCMLeaseToolServices | None = None


def _services() -> SCMLeaseToolServices:
    if _SERVICES is None:
        raise RuntimeError("SCM lease MCP tools are not registered")
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


def acquire_scm_lease(binding_json: str, ctx: Context, project: str = "maxwell") -> str:
    """Broker one exact-binding SCM lease after the host claims the exact wake.

    binding_json binds connection_id, repository, org_id, operations, task_id,
    generation, context_digest, host_id, runner_session_id, work_session_id,
    claim_id, wake_id, and ttl_seconds. No token is returned or stored.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("use:credentials",))
    access = _access(principal)
    payload = _object(binding_json, "binding_json")
    return services.dumps(repository.acquire_lease(
        project=project,
        connection_id=str(payload.get("connection_id") or ""),
        repository=str(payload.get("repository") or ""),
        org_id=str(payload.get("org_id") or ""),
        operations=payload.get("operations") or [],
        task_id=str(payload.get("task_id") or ""),
        generation=str(payload.get("generation") or ""),
        context_digest=str(payload.get("context_digest") or ""),
        host_id=str(payload.get("host_id") or ""),
        runner_session_id=str(payload.get("runner_session_id") or ""),
        work_session_id=str(payload.get("work_session_id") or ""),
        claim_id=str(payload.get("claim_id") or ""),
        wake_id=str(payload.get("wake_id") or ""),
        ttl_seconds=int(payload.get("ttl_seconds") or 900),
        actor=services.principal_actor(principal),
        principal=SCMLeasePrincipal.from_mapping(access)))


def release_scm_lease(lease_id: str, reason: str, ctx: Context,
                      project: str = "maxwell") -> str:
    """Release a live SCM lease before runner drain, replacement, or termination."""
    services = _services()
    principal = services.require_write(ctx, project, ("use:credentials",))
    access = _access(principal)
    return services.dumps(repository.release_lease(
        str(lease_id), project=project, actor=services.principal_actor(principal),
        reason=reason, principal=SCMLeasePrincipal.from_mapping(access)))


def get_scm_lease(lease_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Read one SCM lease's binding and lifecycle state (never a token)."""
    services = _services()
    services.require_read(ctx, project, ("read:credentials",))
    return services.dumps(repository.get_lease(str(lease_id), project=project))


SCM_LEASE_TOOL_NAMES = (
    "acquire_scm_lease",
    "release_scm_lease",
    "get_scm_lease",
)


def register_scm_lease_tools(
        mcp: Any, services: SCMLeaseToolServices) -> dict[str, Callable[..., str]]:
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in SCM_LEASE_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered


__all__ = [
    "SCMLeaseToolServices",
    "SCM_LEASE_TOOL_NAMES",
    "register_scm_lease_tools",
]
