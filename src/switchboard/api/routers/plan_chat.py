"""Queued Ask Taikun REST routes."""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse

import store
from switchboard.storage.repositories import plan_chat as plan_chat_repo


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
        body: dict = Body(...), project: str = Query(store.DEFAULT_PROJECT)
    ):
        """Queue a project-native plan run and return immediately."""
        selected = resolve_project(project)
        message = (body.get("message") or "").strip()
        if not message:
            raise HTTPException(400, "message required")
        session = body.get("session") or "plan"
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in plan_chat_repo.recent_chat(session, 16, project=selected)
            if item.get("content")
        ]
        plan_chat_repo.add_chat(session, "user", message, project=selected)
        try:
            run = store.enqueue_background_job(
                project=selected,
                job_name="plan_agent_run",
                params={
                    "question": message,
                    "history": history,
                    "session": session,
                    "record_chat": True,
                },
                actor="api/ask_plan",
            )
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
                "poll_url": f"api/chat/runs/{run_id}",
            },
        )

    @router.get("/api/chat/history")
    async def plan_chat_history(
        session: str = "plan", project: str = Query(store.DEFAULT_PROJECT)
    ):
        return {
            "messages": plan_chat_repo.recent_chat(
                session, 100, project=resolve_project(project)
            )
        }

    @router.delete("/api/chat")
    async def clear_plan_chat(
        session: str = "plan", project: str = Query(store.DEFAULT_PROJECT)
    ):
        plan_chat_repo.clear_chat(session, project=resolve_project(project))
        return {"cleared": session}

    @router.get("/api/chat/runs/latest")
    async def latest_plan_chat_run(project: str = Query(store.DEFAULT_PROJECT)):
        selected = resolve_project(project)
        listed = store.list_background_job_runs(
            project=selected, job_name="plan_agent_run", limit=1
        )
        rows = listed.get("runs") or []
        if not rows:
            return {"run": None}
        manifest = store.get_background_job_run(
            project=selected, run_id=rows[0]["run_id"]
        )
        if manifest.get("status") in ("pending", "running"):
            store.ensure_background_job_running(
                project=selected, run_id=rows[0]["run_id"], actor="api/ask_plan/resume"
            )
        return {"run": _public_run(manifest)}

    @router.get("/api/chat/runs/{run_id}")
    async def get_plan_chat_run(
        run_id: str, project: str = Query(store.DEFAULT_PROJECT)
    ):
        selected = resolve_project(project)
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
