"""Shared request-scoped trust-boundary helpers (ARCH-MS-70).

Project/principal resolution, control-plane error mapping, and the ETag/JSON
response shape used by several routers (and the global auth middleware) all
live here so the composition root can stay thin. Dependency-light on
purpose: only ``store``/``auth`` and the global auth submodules are needed —
project/principal state lives in the shared SQLite registry, not the
composition root.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException, Request, Response

import auth
import store
from switchboard.api.routers.auth import service as auth_service
from switchboard.api.routers.auth import session as auth_session
from switchboard.api.routers.auth import store as auth_store


ADMIN_SCOPES = [
    "read", "read:credentials", "write:tasks", "write:ixp", "write:system",
    "write:bug_intake", "write:credentials", "use:credentials", "admin",
]


def resolve_project(project: str) -> str:
    """Validate a project id against the registry — fail closed (400) on anything
    unknown so a bad/stale id can never be silently routed to (or written into)
    the wrong db. Empty/missing scope fails closed (SEG-4): never invent
    DEFAULT_PROJECT on customer ingress."""
    from switchboard.application.queries.project_scope import (
        MissingProjectScope,
        UnknownProjectScope,
        require_explicit_project,
    )
    try:
        return require_explicit_project(project, source="query").project_id
    except MissingProjectScope as exc:
        raise HTTPException(400, str(exc)) from exc
    except UnknownProjectScope as exc:
        raise HTTPException(400, str(exc)) from exc


def resolve_principal(request: Request, project: str, scopes=("write:ixp",),
                      dev_actor: str = "web") -> dict:
    resolved = resolve_project(project)
    pre = getattr(request.state, "principal", None)
    if isinstance(pre, dict):
        if auth._has_scopes(pre, scopes, resolved):
            return pre
        raise HTTPException(403, "forbidden: token is missing required scope")
    if not auth.bearer_from_request(request):
        cookies = getattr(request, "cookies", {}) or {}
        user = auth_service.current_user(
            cookies.get(auth_session.COOKIE_NAME, ""))
        if user:
            principal = global_principal(user, global_user_scopes(user, resolved))
            if auth._has_scopes(principal, scopes, resolved):
                return principal
            raise HTTPException(403, "forbidden: token is missing required scope")
    try:
        return auth.authenticate_request(request, resolved, scopes, dev_actor=dev_actor)
    except PermissionError as e:
        status = 403 if "forbidden" in str(e) else 401
        raise HTTPException(status, str(e))


def resolve_agent_host_principal(resolve: Any, request: Request, project: str,
                                 *, dev_actor: str) -> dict:
    """Admit narrow Agent Host bearers plus legacy/operator IXP principals."""
    try:
        return resolve(
            request, project, ("write:agent_host",), dev_actor=dev_actor)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        return resolve(request, project, ("write:ixp",), dev_actor=dev_actor)


def authorize_agent_host_principal(principal: dict, project: str) -> dict:
    """Authorize an already-resolved host principal for rotation recovery."""
    try:
        return auth.authorize_principal(
            principal, project, ("write:agent_host",))
    except PermissionError:
        return auth.authorize_principal(principal, project, ("write:ixp",))


def is_narrow_agent_host_principal(principal: dict) -> bool:
    scopes = set(
        principal.get("effective_scopes") or principal.get("scopes") or [])
    return ("write:agent_host" in scopes and "write:ixp" not in scopes
            and "admin" not in scopes)


def require_agent_host_identity(principal: dict, host_id: str, project: str) -> None:
    """Fence a narrow bearer to the active host identity that owns it."""
    if not is_narrow_agent_host_principal(principal):
        return
    identity = store.check_agent_host_identity(
        str(host_id or "").strip(), str(principal.get("id") or ""),
        project=project)
    if not identity.get("required") or not identity.get("allowed"):
        raise HTTPException(
            403, identity.get("error") or "host bearer is not bound to this host")


def require_agent_host_runner_identity(
        principal: dict, runner_session_id: str, host_id: str, project: str) -> None:
    """Prevent a narrow host bearer from taking over an existing runner id."""
    if not is_narrow_agent_host_principal(principal):
        return
    existing = store.get_runner_session(
        str(runner_session_id or "").strip(), project=project)
    if not existing:
        return
    if (str(existing.get("host_id") or "") != str(host_id or "").strip()
            or str(existing.get("principal_id") or "")
            != str(principal.get("id") or "")):
        raise HTTPException(403, "host bearer cannot replace another runner identity")


def require_personal_execution_authority(
        principal: dict, binding: dict, action: str, project: str) -> dict:
    """Fence a narrow host mutation to its durable personal execution tuple."""
    if not is_narrow_agent_host_principal(principal):
        return {"allowed": True, "legacy_or_operator": True}
    result = store.check_personal_execution_authority(
        binding or {}, principal_id=str(principal.get("id") or ""),
        action=action, project=project)
    if not result.get("allowed"):
        raise HTTPException(403, result)
    return result


def require_agent_host_bootstrap_authority(
        principal: dict, binding: dict, action: str, project: str,
        *, work_session_id: str = "") -> dict:
    """Fence a narrow host to its exact claimed wake and preclaim runner."""
    if not is_narrow_agent_host_principal(principal):
        return {"allowed": True, "legacy_or_operator": True}
    host_id = str((binding or {}).get("host_id") or "").strip()
    require_agent_host_identity(principal, host_id, project)
    result = store.check_agent_host_bootstrap_authority(
        binding or {}, principal_id=str(principal.get("id") or ""),
        project=project, work_session_id=work_session_id, action=action)
    if not result.get("allowed"):
        raise HTTPException(403, result)
    return result


def require_direct_task_completion_authority(
        principal: dict, binding: dict, project: str) -> dict:
    """Fence a native wake to its selected/claimed host and live runner."""
    if not is_narrow_agent_host_principal(principal):
        return {"allowed": True, "legacy_or_operator": True}
    host_id = str((binding or {}).get("host_id") or "").strip()
    require_agent_host_identity(principal, host_id, project)
    result = store.check_direct_task_completion_authority(
        binding or {}, principal_id=str(principal.get("id") or ""),
        project=project)
    if not result.get("allowed"):
        raise HTTPException(403, result)
    return result


def resolve_body_project(body: dict) -> str:
    """Require an explicit body ``project`` — no Maxwell omission fallback (SEG-4)."""
    from switchboard.api.project_scope import resolve_body_project_context
    return resolve_body_project_context(body).project_id


def control_plane_http(result: Any) -> Any:
    if isinstance(result, dict) and result.get("error") == "control_plane_unavailable":
        raise HTTPException(503, result)
    if (isinstance(result, list) and result and isinstance(result[0], dict) and
            result[0].get("error") == "control_plane_unavailable"):
        raise HTTPException(503, result[0])
    return result


def etag_json(request: Request, payload, *, max_age: int) -> Response:
    """Serialize payload to JSON with a weak ETag + short max-age, returning a bodyless
    304 when the client's If-None-Match already matches. The one reused shape behind the
    hot poll endpoints (/api/board + project_context, HARDEN-36/37; and the mission
    pollers, CONSOL-8): a tab refocus/reload — or a 5s live tick that revalidates — skips
    re-downloading an unchanged payload. Pairs with the store's short-TTL read cache: the
    TTL saves the server rebuild, the ETag saves the wire."""
    body = json.dumps(payload, default=str, separators=(",", ":")).encode()
    etag = 'W/"%s"' % hashlib.md5(body).hexdigest()  # noqa: S324 (cache tag, not security)
    headers = {"ETag": etag, "Cache-Control": "private, max-age=%d" % max_age}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


def global_user_scopes(user: dict, project: str) -> list:
    """A global user's effective scopes on a project — superadmin gets admin."""
    if user.get("is_superadmin"):
        return list(ADMIN_SCOPES)
    scopes: set = set()
    for grant in store.principal_project_roles(project, user["id"]):
        scopes.update(grant.get("scopes") or [])
    # ACCESS-15: if the project is in the user's accessible set (owner, invitee, or org
    # membership — including the private→org-admin/owner rule from ACCESS-14), they can at
    # least READ it. Aligns the read gate with the project list so "visible" means "openable";
    # writes still require an explicit role grant.
    accessible = {p.get("id") for p in (user.get("projects") or [])}
    accessible.update(auth_store.accessible_project_ids(
        user["id"], bool(user.get("is_superadmin"))))
    if project in accessible:
        scopes.add("read")
    return sorted(scopes)


def global_principal(user: dict, scopes: list) -> dict:
    return {
        "id": user["id"], "kind": "user",
        "display_name": user.get("display_name") or user.get("email") or user["id"],
        "email": user.get("email"), "scopes": scopes, "effective_scopes": scopes,
        "is_superadmin": bool(user.get("is_superadmin")),
    }
