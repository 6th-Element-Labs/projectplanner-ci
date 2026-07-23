"""Board / roster / plan-health REST routes (ARCH-MS-70).

Owns ``/api/board``, ``/api/people``, ``/api/dispatch/status``,
``/api/signals``, and the IXP REST parity mirror of the saturation dashboard,
while the composition root supplies project resolution and the shared
saturation snapshot / ETag helpers.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import auth
import dispatch
import signals
import store
from switchboard.application.commands import create_task as create_task_command
from switchboard.application.commands import task_execution as task_execution_command


ProjectResolver = Callable[[str], str]
EtagJson = Callable[..., Any]
SaturationSnapshot = Callable[[str], dict]
PrincipalResolver = Callable[..., dict]


def create_router(*, resolve_project: ProjectResolver,
                  etag_json: EtagJson,
                  saturation_snapshot: SaturationSnapshot,
                  resolve_principal: PrincipalResolver | None = None,
                  sibling_bc_only: bool = False) -> APIRouter:
    """Build the board/roster/signals router against shared trust boundaries."""
    router = APIRouter()

    if not sibling_bc_only:
        @router.get("/api/board")
        def board(request: Request, project: str = Query(...),
                  view: str = Query("")):
            # Sync (def, not async) on purpose: FastAPI runs it in the threadpool.
            cards = (view or "").strip().lower() == "cards"
            payload = store.board_payload(resolve_project(project), lite=True, cards=cards)
            # HARDEN-37: short caching avoids repeating the full board payload.
            return etag_json(request, payload, max_age=5)

    @router.get("/api/people")
    async def people(project: str = Query(...)):
        return {"people": store.get_meta(
            "people", store.DEFAULT_PEOPLE, project=resolve_project(project))}

    @router.get("/api/dispatch/status")
    async def dispatch_status(project: str = Query(...)):
        """Is dispatch wired, and is a work-capable agent host online for this project?"""
        return await asyncio.to_thread(dispatch.status, resolve_project(project))

    if not sibling_bc_only:
        @router.get("/api/signals")
        def plan_signals(project: str = Query(...)):
            """Derived plan health and each owner's next-best tasks.

            Sync keeps its SQLite work in FastAPI's threadpool (HARDEN-36).
            """
            return signals.compute_plan_signals(project=resolve_project(project))

    @router.get("/ixp/v1/saturation_signals")
    def ixp_saturation_signals(project: str = Query(...)):
        """REST parity for PERF-7 saturation dashboard (PSI + lock-wait + inbox + SLOs)."""
        return saturation_snapshot(project)

    @router.get("/ixp/v1/open_prs")
    def ixp_open_prs(project: str = Query(...)):
        """Open PRs on the canonical repo with badge-ready status for the fleet dock.

        Sync on purpose (threadpool): the cached path is instant and the cold path
        does network I/O. Degrades to {"prs": [], "unavailable": ...} — never 500s
        a polling dock.
        """
        import open_prs
        return open_prs.open_prs_payload(resolve_project(project))

    @router.get("/ixp/v1/deployments")
    def ixp_deployments(project: str = Query(...)):
        """Recent merged PRs joined to the exact production SHA."""
        import deployment_status
        return deployment_status.deployments_payload(resolve_project(project))

    if resolve_principal is not None:
        @router.post("/api/deployments/request")
        async def request_deployment(request: Request, body: dict = Body(...)):
            """Queue one audited, SHA-pinned deployment-agent task.

            The browser never receives shell or systemd authority. The dispatched
            agent must use the existing fail-closed deploy workflow and verify
            ``/health/version`` before reporting success.
            """
            project = resolve_project(str(body.get("project") or ""))
            principal = resolve_principal(
                request, project, ("write:system",), dev_actor="web")
            import deployment_status
            snapshot = deployment_status.build_deployments(project)
            if snapshot.get("unavailable"):
                raise HTTPException(409, snapshot)
            try:
                pr_number = int(body.get("pr_number") or 0)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "pr_number must be an integer") from exc
            if pr_number <= 0:
                raise HTTPException(400, "pr_number must be positive")
            row = next(
                (item for item in snapshot.get("deployments") or []
                 if int(item.get("number") or 0) == pr_number),
                None,
            )
            if not row:
                raise HTTPException(404, "Merged pull request not found")
            if row.get("deployed"):
                return {"status": "deployed", "deployment": row}
            target_sha = str(snapshot.get("canonical_sha") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{40}", target_sha):
                raise HTTPException(409, "Canonical deployment SHA is unavailable")

            existing = next(
                (task for task in store.list_tasks(
                    workstream="DEPLOY", project=project)
                 if f"[deploy {target_sha[:12]}]" in str(
                     task.get("title") or "").lower()
                 and str(task.get("status") or "") not in
                 deployment_status.TERMINAL_STATUSES),
                None,
            )
            actor = auth.actor(principal)
            if existing:
                task = existing
            else:
                task = create_task_command.execute_mapping_result({
                    "workstream_id": "DEPLOY",
                    "workstream_name": "Production deployments",
                    "title": (
                        f"[deploy {target_sha[:12]}] Deploy canonical master "
                        f"requested from PR #{pr_number}"
                    ),
                    "description": (
                        f"Production-only operation. Deploy canonical SHA {target_sha} "
                        f"for {snapshot.get('repo')} using deploy/auto_deploy.sh or "
                        "deploy/redeploy.sh on the authorized production host. Do not "
                        "change code or open a PR. Fail closed on any readiness or "
                        "runtime-proof error. After the deploy, verify that "
                        "https://plan.taikunai.com/health/version reports running_sha "
                        f"{target_sha}, commits_behind 0, deploy_signal current, and "
                        "last_deploy_ok true. Record the exact production readback."
                    ),
                    "phase": "Deploy",
                    "status": "Not Started",
                    "entry_criteria": (
                        f"Canonical master is {target_sha}; PR #{pr_number} is merged."
                    ),
                    "exit_criteria": (
                        f"Production /health/version confirms running_sha {target_sha} "
                        "and all fail-closed runtime checks pass."
                    ),
                    "deliverable": f"Production deployment of {target_sha}",
                    "risk_level": "High",
                    "is_blocking": True,
                    "ui_impact": "no",
                }, actor=actor, project=project)
                if task.get("error_code"):
                    raise HTTPException(409, task)

            result = await asyncio.to_thread(
                task_execution_command.execute_mapping_result,
                "start_task", task["task_id"], project=project,
                actor=actor, principal_id=principal.get("id") or "",
                role="implementation", runtime="codex",
            )
            if result.get("error_code"):
                raise HTTPException(
                    task_execution_command.error_status(result), result)
            return {
                "status": "queued",
                "task_id": task["task_id"],
                "target_sha": target_sha,
                "execution": result,
            }

    return router
