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
from switchboard.api.routers.auth import store as auth_store


ADMIN_SCOPES = [
    "read", "read:credentials", "write:tasks", "write:ixp", "write:system",
    "write:bug_intake", "write:credentials", "use:credentials", "admin",
]


def resolve_project(project: str) -> str:
    """Validate a project id against the registry — fail closed (400) on anything
    unknown so a bad/stale id can never be silently routed to (or written into)
    the wrong db."""
    if not store.has_project(project):
        raise HTTPException(400, f"unknown project: {project}")
    return project


def resolve_principal(request: Request, project: str, scopes=("write:ixp",),
                      dev_actor: str = "web") -> dict:
    pre = getattr(request.state, "principal", None)
    if isinstance(pre, dict):
        if auth._has_scopes(pre, scopes, resolve_project(project)):
            return pre
        raise HTTPException(403, "forbidden: token is missing required scope")
    try:
        return auth.authenticate_request(request, resolve_project(project), scopes, dev_actor=dev_actor)
    except PermissionError as e:
        status = 403 if "forbidden" in str(e) else 401
        raise HTTPException(status, str(e))


def resolve_body_project(body: dict) -> str:
    return resolve_project((body or {}).get("project") or store.DEFAULT_PROJECT)


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
