"""Access management REST routes (ARCH-MS-51).

Owns ``/api/access/*`` while the composition root supplies project/principal
boundaries and the global-auth account store for invite/member identity.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import store


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
AuthUserLookup = Callable[[str], Any]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  lookup_auth_user: AuthUserLookup,
                  lookup_auth_user_by_email: AuthUserLookup) -> APIRouter:
    """Build the access admin router against shared trust boundaries."""
    router = APIRouter()

    def _resolve_member_identity(subject_kind: str, subject_id: str, principals_by_id: dict) -> dict:
        """Human-readable identity for a grant subject."""
        if subject_kind in ("principal", "agent", "system", "host"):
            p = principals_by_id.get(subject_id) or {}
            return {"display_name": p.get("display_name") or subject_id,
                    "email": None, "revoked": bool(p.get("revoked_at")) if p else None}
        if subject_kind == "user":
            try:
                u = lookup_auth_user(subject_id) or lookup_auth_user_by_email(subject_id)
            except Exception:
                u = None
            if u:
                return {"display_name": u.get("display_name") or u.get("email") or subject_id,
                        "email": u.get("email"), "revoked": None}
        return {"display_name": subject_id,
                "email": subject_id if "@" in subject_id else None, "revoked": None}

    @router.get("/api/access/model")
    async def access_model(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
        principal = resolve_principal(request, project, ("read",), dev_actor="web")
        return store.project_access_model(resolve_project(project), principal_id=principal["id"])


    @router.post("/api/access/project_role")
    async def access_grant_project_role(request: Request, body: dict = Body(...),
                                        project: str = Query(store.DEFAULT_PROJECT)):
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        result = store.grant_project_role(
            resolve_project(project),
            subject_kind=(body or {}).get("subject_kind") or "principal",
            subject_id=(body or {}).get("subject_id") or "",
            role=(body or {}).get("role") or "",
            created_by=auth.actor(principal),
            scopes=store.coerce_csv_list((body or {}).get("scopes")) or None,
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        store.append_activity(
            "access.project_role_granted",
            auth.actor(principal),
            result,
            task_id=None,
            project=resolve_project(project),
        )
        return result



    @router.get("/api/access/members")
    async def access_members(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
        """Members table + private-visibility facts for the UI-5 Members screen (admin-gated).
        Decorates each role grant with a human-readable identity and the audit (granted-by/at)."""
        resolve_principal(request, project, ("write:system",), dev_actor="web")
        proj = resolve_project(project)
        grants = store.list_project_role_grants(proj)
        principals_by_id = {p.get("id"): p for p in
                            store.list_principals(project=proj, include_revoked=True)}
        members = []
        for g in grants:
            ident = _resolve_member_identity(g["subject_kind"], g["subject_id"], principals_by_id)
            members.append({**g, "display_name": ident["display_name"], "email": ident["email"]})
        access = store.project_access(proj)
        return {
            "project": proj,
            "members": members,
            "access": access,
            "visibility": (access.get("visibility") or "org"),
            "owner_user_id": access.get("owner_user_id"),
            "role_definitions": {r: list(s) for r, s in sorted(store.ROLE_SCOPES.items())},
            "global_auth": True,
        }


    @router.post("/api/access/project_role/revoke")
    async def access_revoke_project_role(request: Request, body: dict = Body(...),
                                         project: str = Query(store.DEFAULT_PROJECT)):
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        result = store.revoke_project_role(
            resolve_project(project),
            subject_kind=(body or {}).get("subject_kind") or "principal",
            subject_id=(body or {}).get("subject_id") or "",
            role=(body or {}).get("role") or "",
            created_by=auth.actor(principal),
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        store.append_activity("access.project_role_revoked", auth.actor(principal), result,
                              task_id=None, project=resolve_project(project))
        return result


    @router.post("/api/access/invite")
    async def access_invite(request: Request, body: dict = Body(...),
                            project: str = Query(store.DEFAULT_PROJECT)):
        """Invite a human into this project by email + role. Under global auth this grants the
        role to their existing account (they see the project on next load); pending-invite email
        for not-yet-registered users is ACCESS-5's scope, so we return a clear next step instead."""
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        proj = resolve_project(project)
        email = ((body or {}).get("email") or "").strip().lower()
        role = ((body or {}).get("role") or "").strip().lower()
        if not email or "@" not in email:
            raise HTTPException(400, "a valid email is required")
        if not store.role_scopes(role):
            raise HTTPException(400, f"unknown role: {role}")
        user = lookup_auth_user_by_email(email)
        if not user:
            raise HTTPException(
                404, f"no account for {email} yet — ask them to sign up, then invite again")
        result = store.grant_project_role(proj, subject_kind="user", subject_id=user["id"],
                                          role=role, created_by=auth.actor(principal))
        if result.get("error"):
            raise HTTPException(400, result["error"])
        store.append_activity("access.invited", auth.actor(principal),
                              {"email": email, "user_id": user["id"], "role": role},
                              task_id=None, project=proj)
        return {"project": proj, "grant": result,
                "invited": {"email": email, "user_id": user["id"],
                            "display_name": user.get("display_name")}}


    @router.get("/api/access/tokens")
    async def access_tokens(request: Request, project: str = Query(store.DEFAULT_PROJECT),
                            include_revoked: bool = False, kind: str = ""):
        resolve_principal(request, project, ("write:system",), dev_actor="web")
        return {
            "project": resolve_project(project),
            "tokens": store.list_principals(
                project=resolve_project(project), include_revoked=include_revoked, kind=kind),
            "scope_definitions": store.principal_scope_definitions(),
            "valid_kinds": sorted(store.VALID_PRINCIPAL_KINDS),
        }


    @router.post("/api/access/tokens")
    async def access_create_token(request: Request, body: dict = Body(...),
                                  project: str = Query(store.DEFAULT_PROJECT)):
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        target_project = resolve_project(project)
        resolved = store.resolve_principal_scopes(
            (body or {}).get("scopes"), role=(body or {}).get("role") or "")
        if resolved.get("error"):
            raise HTTPException(400, resolved["error"])
        kind = store.validate_principal_kind((body or {}).get("kind") or "agent")
        if not kind:
            raise HTTPException(400, "kind must be one of: " + ", ".join(sorted(store.VALID_PRINCIPAL_KINDS)))
        raw_token = auth.new_secret_token()
        created = store.create_principal(
            kind=kind,
            display_name=((body or {}).get("display_name") or kind).strip(),
            token=raw_token,
            scopes=resolved["scopes"],
            principal_id=((body or {}).get("principal_id") or None),
            project=target_project,
        )
        if created.get("error"):
            raise HTTPException(400, created["error"])
        public = store.public_principal_record(created, project=target_project)
        store.append_activity(
            "access.token_created",
            auth.actor(principal),
            {"principal": public, "role": resolved.get("role"), "token_returned_once": True},
            task_id=None,
            project=target_project,
        )
        return {"project": target_project, "principal": public, "token": raw_token,
                "token_returned_once": True}


    @router.post("/api/access/tokens/{principal_id}/revoke")
    async def access_revoke_token(principal_id: str, request: Request,
                                  project: str = Query(store.DEFAULT_PROJECT)):
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        result = store.revoke_principal_token(
            principal_id, project=resolve_project(project), actor=auth.actor(principal))
        if result.get("error"):
            raise HTTPException(404 if "not found" in result["error"] else 400, result["error"])
        return result

    return router
