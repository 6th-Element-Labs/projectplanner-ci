"""Shared auth helpers for Switchboard write surfaces.

The first production-safe boundary is intentionally small: bearer credentials map to
principals, writes derive their actor from the authenticated principal, and local/dev
deployments can opt into the old open-write behavior with PM_AUTH_MODE=dev-open.
"""
import os
from typing import Any, Dict, Iterable, Optional

import store


DEV_OPEN = "dev-open"
REQUIRED = "required"


def auth_mode() -> str:
    mode = (os.environ.get("PM_AUTH_MODE") or DEV_OPEN).strip().lower()
    return mode if mode in {DEV_OPEN, REQUIRED} else REQUIRED


def token_hash(token: str) -> str:
    return store.hash_token(token)


def _bearer_from_header(header: str) -> str:
    header = (header or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return header


def bearer_from_request(request: Any) -> str:
    try:
        return _bearer_from_header(request.headers.get("authorization", "") or "")
    except Exception:
        return ""


def bearer_from_mcp_context(ctx: Any) -> str:
    try:
        header = ctx.request_context.request.headers.get("authorization", "") or ""
    except Exception:
        header = ""
    return _bearer_from_header(header)


def _env_principal(token: str, project: str) -> Optional[Dict[str, Any]]:
    """Compatibility bridge for existing single-token deployments.

    PM_MCP_TOKEN already protects the public MCP write surface on plan.taikunai.com.
    PM_AUTH_TOKEN is the matching REST/web write token. Either maps to a system
    principal until explicit principals are created.
    """
    for env_name, principal_id in (("PM_AUTH_TOKEN", "env-auth-token"),
                                   ("PM_MCP_TOKEN", "env-mcp-token")):
        configured = (os.environ.get(env_name) or "").strip()
        if configured and token == configured:
            return {
                "id": principal_id,
                "kind": "system",
                "display_name": principal_id,
                "project": project,
                "scopes": ["read", "write:tasks", "write:ixp", "write:system", "admin"],
            }
    return None


def _has_scopes(principal: Dict[str, Any], required: Iterable[str]) -> bool:
    scopes = set(principal.get("scopes") or [])
    return "admin" in scopes or set(required).issubset(scopes)


def authenticate(project: str, token: str,
                 required_scopes: Iterable[str] = ("write:ixp",),
                 dev_actor: str = "dev-open") -> Dict[str, Any]:
    mode = auth_mode()
    token = (token or "").strip()

    if mode == DEV_OPEN and not token:
        return {
            "id": "dev-open",
            "kind": "system",
            "display_name": dev_actor or "dev-open",
            "project": project,
            "scopes": ["read", "write:tasks", "write:ixp", "write:system", "admin"],
            "dev_open": True,
        }

    principal = store.get_principal_by_token(project, token) if token else None
    if not principal and token:
        principal = _env_principal(token, project)
    if not principal:
        raise PermissionError("unauthorized: provide Authorization: Bearer <token>")
    if principal.get("revoked_at"):
        raise PermissionError("unauthorized: principal revoked")
    if principal.get("project") not in (project, "*"):
        raise PermissionError("unauthorized: token is not valid for this project")
    if not _has_scopes(principal, required_scopes):
        raise PermissionError("forbidden: token is missing required scope")
    return principal


def actor(principal: Dict[str, Any]) -> str:
    return principal.get("display_name") or principal.get("id") or "unknown"

