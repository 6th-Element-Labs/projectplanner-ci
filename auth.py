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
                "project": project,
                "scopes": ["read", "write:tasks", "write:ixp", "write:system", "admin"],
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
    principal["effective_scopes"] = _effective_scopes(principal, project)
    try:
        principal["project_roles"] = store.principal_project_roles(project, principal.get("id") or "")
    except Exception:
        principal["project_roles"] = []
    return principal


def _has_scopes(principal: Dict[str, Any], required: Iterable[str], project: str) -> bool:
    scopes = set(principal.get("effective_scopes") or _effective_scopes(principal, project))
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
    if principal.get("project") not in (project, "*"):
        raise PermissionError("unauthorized: token is not valid for this project")
    principal = _attach_effective_access(principal, project)
    if not _has_scopes(principal, required_scopes, project):
        raise PermissionError("forbidden: token is missing required scope")
    return principal


def authenticate_request(request: Any, project: str,
                         required_scopes: Iterable[str] = ("write:ixp",),
                         dev_actor: str = "dev-open") -> Dict[str, Any]:
    bearer = bearer_from_request(request)
    if bearer:
        return authenticate(project, bearer, required_scopes, dev_actor=dev_actor)

    cookie = session_from_request(request)
    if cookie:
        principal = store.get_principal_by_session(project, cookie)
        if not principal:
            principal = store.get_principal_by_session_any_project(cookie)
            if principal and principal.get("home_project") != project:
                project_roles = store.principal_project_roles(project, principal.get("id") or "")
                if not project_roles:
                    raise PermissionError("unauthorized: session is not valid for this project")
                principal = dict(principal)
                principal["scopes"] = []
        if not principal:
            raise PermissionError("unauthorized: session expired or invalid")
        if principal.get("project") not in (project, "*") and not principal.get("home_project"):
            raise PermissionError("unauthorized: session is not valid for this project")
        principal = _attach_effective_access(principal, project)
        if not _has_scopes(principal, required_scopes, project):
            raise PermissionError("forbidden: session is missing required scope")
        return principal

    return authenticate(project, "", required_scopes, dev_actor=dev_actor)


def verify_login(project: str, login: str, password: str) -> Optional[Dict[str, Any]]:
    row = store.get_password_login(login, project=project)
    if not row or row.get("revoked_at"):
        return None
    if not verify_password(password, row.get("password_hash") or ""):
        return None
    return {
        "id": row["principal_id"],
        "kind": row["kind"],
        "display_name": row["display_name"],
        "project": row["project"],
        "scopes": row["scopes"],
        "effective_scopes": store.effective_principal_scopes(project, row["principal_id"], row["scopes"]),
        "project_roles": store.principal_project_roles(project, row["principal_id"]),
        "login": row["login"],
        "must_rotate": bool(row.get("must_rotate")),
    }


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
