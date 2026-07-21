"""Shared auth helpers for Switchboard access surfaces.

Bearer credentials map to principals for agents/MCP. Human web sessions use
password-backed principals and a hashed, expiring session cookie. Local/dev
deployments can opt into open behavior only with PM_AUTH_MODE=dev-open.
"""
import base64
import hashlib
import hmac
import os
import secrets
from typing import Any, Dict, Iterable, Optional

import store
from constants import MCP_OPERATOR_SCOPES


DEV_OPEN = "dev-open"
REQUIRED = "required"
SESSION_COOKIE = "switchboard_session"
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
_PBKDF2_ITERATIONS = 210_000


def auth_mode() -> str:
    mode = (os.environ.get("PM_AUTH_MODE") or REQUIRED).strip().lower()
    if mode in {"dev", "open", "local"}:
        mode = DEV_OPEN
    return mode if mode in {DEV_OPEN, REQUIRED} else REQUIRED


def token_hash(token: str) -> str:
    return store.hash_token(token)


def session_cookie_name() -> str:
    return (os.environ.get("PM_SESSION_COOKIE_NAME") or SESSION_COOKIE).strip() or SESSION_COOKIE


def session_ttl_seconds() -> int:
    raw = os.environ.get("PM_SESSION_TTL_SECONDS") or ""
    try:
        return max(60, int(raw))
    except Exception:
        return DEFAULT_SESSION_TTL_SECONDS


def new_secret_token() -> str:
    return secrets.token_urlsafe(32)


def password_hash(password: str) -> str:
    password = password or ""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iter_s, salt_s, digest_s = (encoded or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest_s.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            (password or "").encode("utf-8"),
            base64.b64decode(salt_s.encode("ascii")),
            int(iter_s),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


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


def session_from_request(request: Any) -> str:
    try:
        return request.cookies.get(session_cookie_name(), "") or ""
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
                # This is an explicitly configured deployment operator credential,
                # not a project-scoped customer token. SEG-5 owns its eventual
                # rotation; SEG-3 makes the compatibility grant visible in the
                # immutable ProjectContext instead of pretending it is project-bound.
                "project": "*",
                "scopes": list(MCP_OPERATOR_SCOPES),
                "environment_operator": True,
            }
    return None


def _effective_scopes(principal: Dict[str, Any], project: str) -> list:
    try:
        return store.effective_principal_scopes(
            project, principal.get("id") or "", list(principal.get("scopes") or []))
    except Exception:
        return list(principal.get("scopes") or [])


def _attach_effective_access(principal: Dict[str, Any], project: str) -> Dict[str, Any]:
    principal = dict(principal)
    try:
        roles = store.principal_project_roles(project, principal.get("id") or "")
    except Exception:
        roles = []
    principal["project_roles"] = roles
    scopes = set(principal.get("scopes") or [])
    for role in roles:
        scopes.update(role.get("scopes") or [])
    principal["effective_scopes"] = sorted(scopes)
    return principal


def _has_scopes(principal: Dict[str, Any], required: Iterable[str], project: str) -> bool:
    if "effective_scopes" in principal:
        scopes = set(principal.get("effective_scopes") or [])
    else:
        scopes = set(_effective_scopes(principal, project))
    return "admin" in scopes or set(required).issubset(scopes)


def authorize_principal(principal: Dict[str, Any], project: str,
                        required_scopes: Iterable[str] = ("read",)) -> Dict[str, Any]:
    """Authorize an already-authenticated principal for exactly one project.

    This is deliberately separate from bearer lookup. Project-bound principals
    cannot reuse their base scopes on another project. Cross-project access is
    allowed only through an active registry grant (whose creator/time remain in
    ``project_roles``), a superadmin account, or the explicit environment operator
    compatibility credential.
    """
    selected = (project or "").strip()
    if not selected or not store.has_project(selected):
        raise PermissionError(f"forbidden: unknown project: {selected or '<missing>'}")
    principal = dict(principal or {})
    if not principal.get("id"):
        raise PermissionError("unauthorized: authenticated principal is missing")
    if principal.get("revoked_at"):
        raise PermissionError("unauthorized: principal revoked")

    binding = str(principal.get("project") or "").strip()
    try:
        roles = store.principal_project_roles(selected, principal.get("id") or "")
    except Exception:
        roles = []
    environment_operator = bool(principal.get("environment_operator"))
    superadmin = bool(principal.get("is_superadmin"))
    same_project = binding == selected
    explicit_cross_project = bool(roles)
    if not (same_project or environment_operator or superadmin or explicit_cross_project):
        raise PermissionError("forbidden: token is not valid for this project")

    # Base token scopes are valid only on the token's own project. A cross-project
    # role grant contributes only its recorded scopes, so a global/admin base scope
    # cannot silently widen a viewer grant.
    scopes = set(principal.get("scopes") or []) if (
        same_project or environment_operator or superadmin) else set()
    for role in roles:
        scopes.update(role.get("scopes") or [])
    principal["effective_scopes"] = sorted(scopes)
    principal["project_roles"] = roles
    principal["authorized_project"] = selected
    if not _has_scopes(principal, required_scopes, selected):
        raise PermissionError("forbidden: token is missing required scope")
    return principal


def accessible_project_ids_for_principal(principal: Dict[str, Any]) -> list[str]:
    """Filtered MCP discovery set from one principal + one registry query."""
    principal = dict(principal or {})
    if not principal.get("id") or principal.get("revoked_at"):
        return []
    all_ids = list(store.project_ids())
    if (principal.get("environment_operator") or principal.get("is_superadmin") or
            principal.get("dev_open")):
        return all_ids
    grants = store.principal_project_grants(principal.get("id") or "")
    allowed = {str(grant.get("project_id") or "") for grant in grants
               if "read" in set(grant.get("scopes") or []) or
               "admin" in set(grant.get("scopes") or [])}
    binding = str(principal.get("project") or "").strip()
    if binding and binding != "*" and (
            "read" in set(principal.get("scopes") or []) or
            "admin" in set(principal.get("scopes") or [])):
        allowed.add(binding)
    return [project_id for project_id in all_ids if project_id in allowed]


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
            "scopes": list(MCP_OPERATOR_SCOPES),
            "dev_open": True,
        }

    principal = store.get_principal_by_token(project, token) if token else None
    if not principal and token:
        principal = store.get_principal_by_token_any_project(token)
        binding = (principal or {}).get("project")
        if binding not in (project, "*"):
            principal = None
    if not principal and token:
        principal = _env_principal(token, project)
    if not principal:
        raise PermissionError("unauthorized: provide Authorization: Bearer <token>")
    if principal.get("revoked_at"):
        raise PermissionError("unauthorized: principal revoked")
    return authorize_principal(principal, project, required_scopes)


def principal_for_token_any_project(token: str) -> Optional[Dict[str, Any]]:
    """Transport-level authentication: does this bearer map to any known, live principal?

    Used by the MCP request middleware (BUG-46) to reject anonymous callers before any tool
    runs — reads used to bypass auth entirely, exposing project/task/activity data to anyone
    who could reach /mcp. This answers only "is the caller a real principal"; per-project and
    per-scope authorization still happen inside each tool via authenticate(). A store lookup
    error falls through to the env-token bridge rather than failing the whole surface closed
    on a transient DB blip. Returns the principal on success, or None when the token is
    missing, unknown, or revoked.
    """
    token = (token or "").strip()
    if not token:
        return None
    try:
        principal = store.get_principal_by_token_any_project(token)
    except Exception:
        principal = None
    if not principal:
        try:
            principal = store.get_principal_by_work_session_token_any_project(token)
        except Exception:
            principal = None
    if not principal:
        try:
            principal = store.get_direct_session_principal_by_token_any_project(token)
        except Exception:
            principal = None
    if not principal:
        principal = _env_principal(token, "*")
    if not principal:
        return None
    if principal.get("revoked_at"):
        return None
    return principal


def authenticate_request(request: Any, project: str,
                         required_scopes: Iterable[str] = ("write:ixp",),
                         dev_actor: str = "dev-open") -> Dict[str, Any]:
    bearer = bearer_from_request(request)
    return authenticate(project, bearer, required_scopes, dev_actor=dev_actor)


def actor(principal: Dict[str, Any]) -> str:
    return principal.get("display_name") or principal.get("id") or "unknown"


def public_principal(principal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": principal.get("id"),
        "kind": principal.get("kind"),
        "display_name": principal.get("display_name"),
        "project": principal.get("project"),
        "scopes": list(principal.get("scopes") or []),
        "effective_scopes": list(principal.get("effective_scopes") or principal.get("scopes") or []),
        "project_roles": list(principal.get("project_roles") or []),
        "login": principal.get("login"),
        "session_id": principal.get("session_id"),
        "session_expires_at": principal.get("session_expires_at"),
    }
