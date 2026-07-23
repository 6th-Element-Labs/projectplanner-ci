"""Project lifecycle read routes."""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response

import auth
import comms
import notify
import store
from switchboard.application.commands import (
    project_consolidation,
    project_lifecycle,
    project_metadata,
    project_purge,
)
from switchboard.application.queries import project_admin, project_impact


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
CurrentUserResolver = Callable[[str], Optional[dict]]
AccessibleProjectIds = Callable[[str, bool], Any]
EtagJson = Callable[..., Response]
WebhookSecretConfigured = Callable[[], bool]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  current_user: CurrentUserResolver,
                  cookie_name: str,
                  accessible_project_ids: AccessibleProjectIds,
                  etag_json: EtagJson,
                  webhook_secret_configured: WebhookSecretConfigured) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects")
    def list_projects(request: Request, include_archived: bool = Query(False)):
        """Active picker by default; explicit admin discovery may include archived records.

        Cookie sessions stay deny-by-default (grants only). Bearer principals — env MCP/auth
        tokens and scoped agent tokens — must also work here so the boot picker matches
        ``/api/board`` and MCP ``list_projects`` (Playwright audit BUG-A1 / ACCESS-25).
        """
        if auth.auth_mode() == auth.DEV_OPEN and not request.cookies.get(cookie_name, ""):
            projects = (store.list_registry_projects(include_archived=True)
                        if include_archived else store.projects())
            return {"projects": projects, "default": "",
                    "include_archived": include_archived}
        user = current_user(request.cookies.get(cookie_name, ""))
        if user:
            if not include_archived:
                return {"projects": user.get("projects", []), "default": "",
                        "include_archived": False}
            accessible = set(accessible_project_ids(
                user["id"], bool(user.get("is_superadmin"))))
            projects = [
                record for record in store.list_registry_projects(include_archived=True)
                if record.get("id") in accessible
            ]
            return {"projects": projects, "default": "", "include_archived": True}

        # Prefer the principal the auth gate already attached (Bearer path).
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, dict):
            principal = auth.principal_for_token_any_project(auth.bearer_from_request(request))
        if not principal:
            raise HTTPException(401, "not authenticated")
        binding = (principal.get("project") or "").strip()
        broad = (
            principal.get("id") in ("env-mcp-token", "env-auth-token")
            or binding in ("", "*")
        )
        if not include_archived:
            projects = store.projects()
            if not broad:
                projects = [p for p in projects if p.get("id") == binding]
            return {
                "projects": projects,
                "default": "",
                "include_archived": False,
            }
        records = store.list_registry_projects(include_archived=True)
        if not broad:
            records = [r for r in records if r.get("id") == binding]
        return {"projects": records, "default": "", "include_archived": True}

    @router.post("/api/projects")
    async def create_project(request: Request, body: dict = Body(...)):
        # ACCESS-14: contributors (write:projects) can create projects, not just admins.
        # Human-created projects default to private (creator + invitees + org admins see them);
        # pass visibility="org" to make one org-wide shared.
        principal = resolve_principal(request, "switchboard", ("write:projects",), dev_actor="web")
        created = store.create_project(
            name=body.get("name") or body.get("label") or "",
            project_id=body.get("project_id") or body.get("id") or "",
            label=body.get("label") or "",
            pretitle=body.get("pretitle") or "",
            github_repo=body.get("github_repo") or body.get("repo") or "",
            owner_principal_id=principal["id"],
            org_id=body.get("org_id") or store.DEFAULT_ORG_ID,
            purpose=body.get("purpose") or "",
            boundary=body.get("boundary") or "",
            visibility=(body.get("visibility") or "private").strip().lower(),
            actor=auth.actor(principal),
        )
        if created.get("error"):
            raise HTTPException(400, created["error"])
        return created

    @router.get("/api/projects/{project}/repo_topology")
    async def project_repo_topology(project: str):
        return store.get_project_repo_topology(project=resolve_project(project))

    @router.get("/api/projects/{project}/execution_policy")
    async def project_execution_policy(project: str):
        """ACCESS-27: the runner authority plus its typed readiness gate."""
        return store.get_project_execution_policy(project=resolve_project(project))

    @router.post("/api/projects/{project}/execution_policy")
    async def set_project_execution_policy(request: Request, project: str,
                                           body: dict = Body(...)):
        """Merge an execution-policy update. Rejected updates persist nothing."""
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        result = store.set_project_execution_policy(
            project=resolve_project(project),
            updates=body if isinstance(body, dict) else {},
            actor=auth.actor(principal))
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.get("/api/projects/{project}/context")
    def project_context(request: Request, project: str):
        # HARDEN-35: project_context (repo roles, hierarchy, policy profiles) is a
        # near-static ~9KB blob that used to ride on every /api/board load. It lives
        # here now so the board payload stays slim; ETag + a short max-age let a tab
        # refocus / reload reuse the browser-cached copy (bodyless 304). Sync def so
        # its SQLite I/O runs in the threadpool, like /api/board (HARDEN-36).
        payload = store.get_project_context(project=resolve_project(project))
        return etag_json(request, payload, max_age=60)

    @router.post("/api/projects/{project}/repo_topology")
    async def set_project_repo_topology(request: Request, project: str, body: dict = Body(...)):
        resolve_principal(request, "switchboard", ("write:system",), dev_actor="web")
        result = store.set_project_repo_topology(
            project=resolve_project(project),
            canonical_repo=body.get("canonical_repo") or body.get("private_repo") or "",
            public_ci_repo=body.get("public_ci_repo") or body.get("ci_repo") or "",
            public_repo=body.get("public_repo") or "",
            release_repo=body.get("release_repo") or "",
            topology_type=body.get("topology_type") or "",
            canonical_default_branch=body.get("canonical_default_branch") or body.get("default_branch") or "",
            canonical_claim_gate=body.get("canonical_claim_gate") or body.get("claim_gate") or "",
            public_ci_required_status_contexts=(
                body.get("public_ci_required_status_contexts") or
                body.get("ci_required_status_contexts") or
                body.get("required_status_contexts") or
                ""
            ),
            public_ci_sync_scripts=(
                body.get("public_ci_sync_scripts") or
                body.get("ci_sync_scripts") or
                body.get("sync_scripts") or
                ""
            ),
            public_publish_scripts=body.get("public_publish_scripts") or body.get("publish_scripts") or "",
            release_publish_scripts=body.get("release_publish_scripts") or "",
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.get("/api/projects/{project}/github_association")
    def project_github_association(request: Request, project: str, check: int = 0):
        """UI-15: everything the "Wire your repo" panel needs — the webhook payload URL with
        the ?project= pin PRE-FILLED (HARDEN-2/BUG-24: bare URLs fail closed on shared repos),
        the secret name, a copyable gh one-liner, and delivery-based verification. Pass ?check=1
        (the Verify button) to also probe repo reachability; the panel open path omits it so it
        never makes a network call until the operator asks."""
        project = resolve_project(project)
        repo = store.get_project_github_repo(project) or ""
        base = str(request.base_url).rstrip("/")
        payload_url = f"{base}/api/github/webhook?project={project}"
        gh_command = ""
        if repo:
            gh_command = (
                f"gh api -X POST repos/{repo}/hooks -f name=web -F active=true "
                f"-f 'events[]=push' -f 'events[]=pull_request' "
                f"-f 'events[]=check_run' -f 'events[]=check_suite' -f 'events[]=status' "
                f"-f 'config[url]={payload_url}' -f config[content_type]=json "
                f"-f 'config[secret]=$PM_GITHUB_WEBHOOK_SECRET'"
            )
        deliveries = store.github_webhook_deliveries(project)
        reachable = store.github_repo_reachable(repo) if (check and repo) else None
        status = "connected" if deliveries["delivered"] else ("configured" if repo else "unconfigured")
        return {
            "project": project,
            "repo": repo,
            "repo_configured": bool(repo),
            "webhook": {
                "payload_url": payload_url,
                "content_type": "application/json",
                "secret_env": "PM_GITHUB_WEBHOOK_SECRET",
                "secret_configured": webhook_secret_configured(),
                "events": ["push", "pull_request", "check_run", "check_suite", "status"],
                "gh_command": gh_command,
            },
            "verification": {**deliveries, "status": status, "repo_reachable": reachable},
        }

    @router.post("/api/projects/{project}/github_repo")
    async def set_project_github_repo_route(request: Request, project: str, body: dict = Body(...)):
        """UI-15: record/replace a project's canonical repo from the web (Settings path for
        existing projects). Reroutes Done/webhook provenance, so it is gated like repo_topology."""
        project = resolve_project(project)
        resolve_principal(request, project, ("write:system",), dev_actor="web")
        result = store.set_project_github_repo(
            repo=body.get("github_repo") or body.get("repo") or "", project=project)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.get("/api/projects/{project}/comms")
    def project_comms(request: Request, project: str):
        """UI-14: everything the Settings → Communications screen needs — the project's plus-address,
        its associated inbound domains (the editable UI-13 routing map), per-project digest/notify
        recipients + cadence, the global .env fallback, and channel status. Readable to anyone who can
        read the project; edits below are admin-gated."""
        project = resolve_project(project)
        cfg = comms.get_config(project)
        # Reflect whether THIS caller may edit, so the UI can disable Save/Test up front instead of
        # only failing on POST. Non-raising probe of the same scope the write routes require.
        try:
            auth.authenticate_request(request, project, ("write:system",), dev_actor="web")
            cfg["can_edit"] = True
        except PermissionError:
            cfg["can_edit"] = False
        return cfg

    @router.post("/api/projects/{project}/comms")
    async def set_project_comms(request: Request, project: str, body: dict = Body(...)):
        """UI-14: persist a Communications edit — associated inbound domains and/or outbound
        recipients/cadence. Reroutes inbound mail and outbound recipients, so it is admin-gated
        (write:system, same as repo settings) and audited."""
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        result = comms.update_config(body or {}, project=project, actor=auth.actor(principal))
        if result.get("error"):
            raise HTTPException(400, result["error"])
        store.append_activity("comms.updated", auth.actor(principal),
                              result.get("audit") or {}, project=project)
        return result

    @router.post("/api/projects/{project}/comms/test")
    async def test_project_comms(request: Request, project: str, body: dict = Body(...)):
        """UI-14 Send-test: email the project's effective recipients so an operator can confirm the
        wiring end-to-end. Admin-gated + audited; dry-runs (logs, sent=false) until SMTP is configured."""
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:system",), dev_actor="web")
        kind = (body or {}).get("kind") or "notify"
        if kind not in ("notify", "digest"):
            raise HTTPException(400, "kind must be 'notify' or 'digest'")
        recipients = comms.recipients_for(project, kind) or comms.global_fallback_recipients()
        subject = f"{project} — communications test"
        text = (f"Communications test from plan.taikunai.com for project '{project}'. "
                f"If you received this, {project}'s {kind} recipients are wired correctly.")
        results = await asyncio.to_thread(notify.send, subject, text, ("email",), project, kind)
        store.append_activity("comms.test_sent", auth.actor(principal),
                              {"kind": kind, "recipients": recipients, "results": results},
                              project=project)
        return {"project": project, "kind": kind, "recipients": recipients, "results": results}

    @router.get("/api/projects/{project}/boards")
    async def list_project_boards(project: str, kind: str = "", status: str = ""):
        project = resolve_project(project)
        return {"project": project, "boards": store.list_project_boards(
            project=project, kind=kind, status=status)}

    @router.post("/api/projects/{project}/boards")
    async def create_project_board(request: Request, project: str, body: dict = Body(...)):
        project = resolve_project(project)
        principal = resolve_principal(request, project, ("write:tasks",), dev_actor="web")
        result = store.create_project_board(body or {}, actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.get("/api/projects/{project}/boards/{board_id}")
    async def get_project_board(project: str, board_id: str):
        project = resolve_project(project)
        result = store.get_project_board(board_id, project=project)
        if not result:
            raise HTTPException(404, "board not found")
        return result

    @router.get("/api/projects/{project}")
    def get_project(request: Request, project: str):
        project_id = resolve_project(project)
        principal = resolve_principal(request, project_id, ("read",), dev_actor="web")
        result = project_admin.execute_for(
            project_id,
            access_repository=store.access_repository,
            repo_topology_provider=store.get_project_repo_topology,
            access_model_provider=store.project_access_model,
            principal_id=str(principal.get("id") or ""),
            principal_scopes=list(
                principal.get("effective_scopes") or principal.get("scopes") or []),
        )
        if result.get("error"):
            raise HTTPException(404, result)
        return result

    @router.patch("/api/projects/{project}")
    def update_project(request: Request, project: str,
                       body: dict = Body(...)):
        project_id = resolve_project(project)
        trust_boundary = bool({"boundary", "visibility"}.intersection(body or {}))
        principal = resolve_principal(
            request, project_id,
            (("write:system",) if trust_boundary else ("write:projects",)),
            dev_actor="web")
        result = project_metadata.execute(
            {**dict(body or {}), "project_id": project_id},
            actor=auth.actor(principal),
            access_repository=store.access_repository,
        )
        if result.get("error"):
            code = 423 if result.get("error") == "project_archived" else 400
            if str(result.get("error")).startswith("unknown project"):
                code = 404
            raise HTTPException(code, result)
        return result

    @router.get("/api/projects/{project}/impact")
    def project_impact_report(request: Request, project: str,
                              limit: int = Query(50, ge=1, le=200)):
        project_id = resolve_project(project)
        resolve_principal(request, project_id, ("read",), dev_actor="web")
        result = project_impact.execute_for(
            project_id,
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
            limit=limit,
        )
        if result.get("error"):
            raise HTTPException(404, result)
        return result

    @router.post("/api/projects/{project}/archive")
    def archive_project(request: Request, project: str,
                        body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_lifecycle.archive_project(
            {**dict(body or {}), "project_id": project_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            code = 404 if str(result.get("error")).startswith("unknown project") else 409
            if str(result.get("error")).startswith("invalid_archive"):
                code = 400
            raise HTTPException(code, result)
        return result

    @router.post("/api/projects/{project}/restore")
    def restore_project(request: Request, project: str,
                        body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_lifecycle.restore_project(
            {**dict(body or {}), "project_id": project_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            code = 404 if str(result.get("error")).startswith("unknown project") else 409
            if str(result.get("error")).startswith("invalid_restore"):
                code = 400
            raise HTTPException(code, result)
        return result

    @router.post("/api/projects/{project}/consolidation/plan")
    def plan_project_consolidation(request: Request, project: str,
                                   body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_consolidation.plan_project_consolidation(
            {**dict(body or {}), "source_project_id": project_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            code = 404 if str(result.get("error")).startswith("unknown project") else 409
            if str(result.get("error")).startswith("invalid_project_consolidation"):
                code = 400
            raise HTTPException(code, result)
        return result

    @router.post("/api/projects/{project}/consolidation/apply")
    def apply_project_consolidation(request: Request, project: str,
                                    body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        plan = dict((body or {}).get("plan") or {})
        if plan.get("source_project_id") != project_id:
            raise HTTPException(400, {"error": "plan source does not match route project"})
        result = project_consolidation.apply_project_consolidation(
            {**dict(body or {}), "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    @router.get("/api/projects/{project}/consolidation/{consolidation_id}/verify")
    def verify_project_consolidation(request: Request, project: str,
                                     consolidation_id: str):
        project_id = resolve_project(project)
        resolve_principal(request, project_id, ("read",), dev_actor="web")
        result = project_consolidation.verify_project_consolidation(
            project_id, consolidation_id,
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(404, result)
        return result

    @router.post("/api/projects/{project}/consolidation/{consolidation_id}/rollback")
    def rollback_project_consolidation(request: Request, project: str,
                                       consolidation_id: str,
                                       body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_consolidation.rollback_project_consolidation(
            {**dict(body or {}), "source_project_id": project_id,
             "consolidation_id": consolidation_id, "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    @router.post("/api/projects/{project}/purge/intents")
    def create_project_purge_intent(request: Request, project: str,
                                    body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_purge.create_purge_intent(
            {**dict(body or {}), "project_id": project_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409 if not str(result["error"]).startswith("invalid_") else 400,
                                result)
        return result

    @router.post("/api/projects/{project}/purge/intents/{intent_id}/verify")
    def verify_project_purge_intent(request: Request, project: str, intent_id: str,
                                    body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_purge.verify_purge_intent(
            {**dict(body or {}), "project_id": project_id, "intent_id": intent_id,
             "verifier": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    @router.post("/api/projects/{project}/purge/intents/{intent_id}/execute")
    def execute_project_purge(request: Request, project: str, intent_id: str,
                              body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        result = project_purge.execute_purge(
            {**dict(body or {}), "project_id": project_id, "intent_id": intent_id,
             "actor": auth.actor(principal)},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    @router.post("/api/projects/{project}/cleanup-review")
    def record_project_cleanup_review(request: Request, project: str,
                                      body: dict = Body(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:system",), dev_actor="web")
        actor_name = auth.actor(principal)
        result = project_purge.record_cleanup_review(
            {**dict(body or {}), "project_id": project_id,
             "approved_by": actor_name},
            access_repository=store.access_repository,
            project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
        )
        if result.get("error"):
            raise HTTPException(409, result)
        return result

    return router
