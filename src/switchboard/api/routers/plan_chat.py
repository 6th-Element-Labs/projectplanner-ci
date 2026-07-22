"""Queued Ask Taikun REST routes."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import store
from switchboard.api.project_scope import (
    bind_ask_taikun_context,
    create_ask_taikun_context,
)
from switchboard.storage.repositories import plan_chat as plan_chat_repo
from switchboard.storage.repositories import ai_admission


ProjectResolver = Callable[[str], str]


def _public_run(manifest: dict) -> dict:
    payload = {
        "run_id": manifest.get("run_id"),
        "project": manifest.get("project"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "error": manifest.get("error"),
    }
    if manifest.get("status") == "completed":
        steps = manifest.get("steps") or []
        result = (steps[0].get("result") or {}) if steps else {}
        payload.update(result)
    return payload


def create_router(*, resolve_project: ProjectResolver) -> APIRouter:
    router = APIRouter()

    @router.post("/api/chat")
    async def plan_chat(
        request: Request,
        body: dict = Body(...),
        project: str = Query(...),
    ):
        """Queue a project-native plan run and return immediately."""
        message = (body.get("message") or "").strip()
        if not message:
            raise HTTPException(400, "message required")
        session = body.get("session") or "plan"
        ctx = create_ask_taikun_context(request, project=project, session=session)
        selected = resolve_project(ctx.project_id)
        principal = getattr(request.state, "principal", None) or {}
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in plan_chat_repo.recent_chat(session, 16, project=selected)
            if item.get("content")
        ]
        try:
            authorization = ai_admission.authorization_snapshot(principal)
            decision = ai_admission.admit(
                project=selected, surface="browser_chat", authorization=authorization,
                question=message)
            plan_chat_repo.add_chat(session, "user", message, project=selected)
            run = store.enqueue_background_job(
                project=selected,
                job_name="plan_agent_run",
                params={
                    "question": message,
                    "history": history,
                    "session": session,
                    "record_chat": True,
                    "ai_admission_id": decision.admission_id,
                    "ai_authorization": authorization,
                },
                actor="api/ask_plan",
                start_worker=decision.status == ai_admission.ACTIVE,
            )
            ai_admission.bind_run(selected, decision.admission_id, run["run_id"])
        except ai_admission.AdmissionDenied as exc:
            status = 429 if exc.decision.reason_code in {
                "queue_capacity", "hourly_prompt_limit", "daily_prompt_limit",
                "prompt_too_large",
            } else 403
            raise HTTPException(status, {"reason_code": exc.decision.reason_code}) from exc
        except Exception as exc:
            plan_chat_repo.add_chat(
                session, "assistant", f"(agent queue error: {exc})", project=selected
            )
            raise HTTPException(503, f"agent queue error: {exc}") from exc
        run_id = run["run_id"]
        return JSONResponse(
            status_code=202,
            content={
                "run_id": run_id,
                "project": selected,
                "status": "pending",
                "poll_url": f"api/chat/runs/{run_id}?project={selected}",
            },
        )

    @router.get("/api/chat/history")
    async def plan_chat_history(
        request: Request,
        session: str = "plan",
        project: str = Query(...),
    ):
        ctx = bind_ask_taikun_context(request, project=project, session=session)
        selected = resolve_project(ctx.project_id)
        return {
            "messages": plan_chat_repo.recent_chat(session, 100, project=selected),
            "project": selected,
            "session": session,
        }

    @router.delete("/api/chat")
    async def clear_plan_chat(
        request: Request,
        session: str = "plan",
        project: str = Query(...),
    ):
        ctx = bind_ask_taikun_context(request, project=project, session=session)
        selected = resolve_project(ctx.project_id)
        plan_chat_repo.clear_chat(session, project=selected)
        return {"cleared": session, "project": selected}

    @router.get("/api/chat/runs/latest")
    async def latest_plan_chat_run(
        request: Request,
        session: str = "plan",
        project: str = Query(...),
    ):
        ctx = bind_ask_taikun_context(request, project=project, session=session)
        selected = resolve_project(ctx.project_id)
        listed = store.list_background_job_runs(
            project=selected, job_name="plan_agent_run", limit=1
        )
        rows = listed.get("runs") or []
        if not rows:
            return {"run": None, "project": selected}
        manifest = store.get_background_job_run(
            project=selected, run_id=rows[0]["run_id"]
        )
        if manifest.get("status") in ("pending", "running"):
            store.ensure_background_job_running(
                project=selected, run_id=rows[0]["run_id"], actor="api/ask_plan/resume"
            )
        return {"run": _public_run(manifest), "project": selected}

    @router.get("/api/chat/runs/{run_id}")
    async def get_plan_chat_run(
        request: Request,
        run_id: str,
        session: str = "plan",
        project: str = Query(...),
    ):
        ctx = bind_ask_taikun_context(request, project=project, session=session)
        selected = resolve_project(ctx.project_id)
        manifest = store.get_background_job_run(project=selected, run_id=run_id)
        if manifest.get("error") == "run_not_found":
            raise HTTPException(404, manifest["error"])
        if manifest.get("job_name") != "plan_agent_run":
            raise HTTPException(404, "plan run not found")
        if manifest.get("status") in ("pending", "running"):
            store.ensure_background_job_running(
                project=selected, run_id=run_id, actor="api/ask_plan/resume"
            )
        return _public_run(manifest)

    return router
