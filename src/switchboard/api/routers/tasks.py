"""Task REST routes.

The router owns the complete ``/api/tasks`` HTTP surface while the composition
root supplies the shared project and principal boundaries.  Task creation,
reads, and updates continue to delegate to the application layer.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import APIRouter, Body, HTTPException, Query, Request

import agent
import auth
import dispatch
import store
from switchboard.application.commands import create_task as create_task_command
from switchboard.application.commands import move_task as move_task_command
from switchboard.application.commands import update_task as update_task_command
from switchboard.application.queries import get_task as get_task_query


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver) -> APIRouter:
    """Build the task router against the monolith's shared trust boundaries."""
    router = APIRouter()

    def resolve_write_actor(request: Request, project: str, body: dict,
                            task_id: str = "", scopes=("write:tasks",)) -> dict:
        principal = resolve_principal(request, project, scopes, dev_actor="web")
        binding = store.resolve_write_actor(
            auth.actor(principal),
            project=project,
            task_id=task_id,
            agent_id=(body or {}).get("agent_id") or "",
            system_actor=(body or {}).get("system_actor") or "",
            system_reason=(body or {}).get("system_reason") or "",
            principal_id=principal.get("id") or "",
        )
        if not binding.get("ok"):
            raise HTTPException(409, binding)
        return binding

    def record_write_binding(task_id: str, binding: dict, project: str) -> None:
        if not task_id or not isinstance(binding, dict):
            return
        if binding.get("binding") in ("principal", None):
            return
        store.append_activity(
            "principal.write_bound",
            "switchboard/identity",
            store.write_binding_activity_payload(binding),
            task_id=task_id,
            project=project,
        )

    def without_write_binding_fields(body: dict) -> dict:
        clean = dict(body or {})
        for key in ("agent_id", "system_actor", "system_reason"):
            clean.pop(key, None)
        return clean

    @router.get("/api/tasks")
    async def list_tasks(workstream: str = None, status: str = None, assignee: str = None,
                         project: str = Query(store.DEFAULT_PROJECT)):
        return {"tasks": store.list_tasks(workstream, status, assignee,
                                          project=resolve_project(project))}

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, project: str = Query(store.DEFAULT_PROJECT)):
        task = get_task_query.execute_for(task_id, project=resolve_project(project))
        if not task:
            raise HTTPException(404, "task not found")
        return task

    @router.post("/api/tasks")
    async def create_task(request: Request, body: dict = Body(...),
                          project: str = Query(...)):
        project = resolve_project(project)
        binding = resolve_write_actor(request, project, body)
        task = create_task_command.execute_mapping_result(
            without_write_binding_fields(body), actor=binding["actor"], project=project)
        if task.get("error"):
            raise HTTPException(400, task)
        record_write_binding(task.get("task_id") or "", binding, project)
        return task

    @router.patch("/api/tasks/{task_id}")
    async def patch_task(request: Request, task_id: str, body: dict = Body(...),
                         project: str = Query(...)):
        project = resolve_project(project)
        body = dict(body or {})
        binding = resolve_write_actor(request, project, body, task_id=task_id)
        task = update_task_command.execute_mapping_result(
            task_id, without_write_binding_fields(body),
            actor=binding["actor"], project=project)
        if not task:
            raise HTTPException(404, "task not found")
        if task.get("error") == "done_requires_merge_provenance":
            raise HTTPException(409, task.get("message") or "Done requires merge provenance")
        if task.get("error"):
            raise HTTPException(400, task)
        record_write_binding(task_id, binding, project)
        return task

    @router.post("/api/tasks/{task_id}/verify_offline")
    async def verify_task_offline(request: Request, task_id: str,
                                  body: dict = Body(default={}),
                                  project: str = Query(...)):
        project = resolve_project(project)
        body = dict(body or {})
        binding = resolve_write_actor(request, project, body, task_id=task_id)
        actor_name = binding["actor"]
        result = store.mark_task_offline_done(
            task_id,
            evidence=body.get("evidence") or body.get("evidence_json") or {},
            artifact_url=body.get("artifact_url") or "",
            evidence_hash=body.get("evidence_hash") or body.get("hash") or "",
            verifier=body.get("verifier") or actor_name,
            reviewed_at=body.get("reviewed_at"),
            actor=actor_name,
            project=project,
        )
        if result.get("error") == "task not found":
            raise HTTPException(404, result)
        if result.get("error"):
            raise HTTPException(409, result)
        record_write_binding(task_id, binding, project)
        return result

    @router.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str, project: str = Query(...)):
        if not store.delete_task(task_id, project=resolve_project(project)):
            raise HTTPException(404, "task not found")
        return {"deleted": task_id}

    @router.post("/api/tasks/{task_id}/archive")
    async def archive_task(request: Request, task_id: str,
                           body: dict = Body(default={}), project: str = Query(...)):
        project = resolve_project(project)
        principal = resolve_principal(
            request, "switchboard", ("write:system",), dev_actor="web")
        result = store.archive_task(
            task_id, reason=(body or {}).get("reason") or "",
            actor=auth.actor(principal), project=project)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/tasks/{task_id}/move")
    async def move_task(request: Request, task_id: str, body: dict = Body(...),
                        project: str = Query(...)):
        project_from = resolve_project(project)
        body = dict(body or {})
        # Resolve destination through the same project gate as the query arg,
        # then hand a transport-neutral payload to the shared command.
        destination = body.get("project_to") or body.get("destination_project") or ""
        body["project_from"] = project_from
        body["project_to"] = resolve_project(destination) if destination else ""
        principal = resolve_principal(
            request, "switchboard", ("write:system",), dev_actor="web")
        result = move_task_command.execute_mapping_result(
            task_id, body, actor=auth.actor(principal))
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/tasks/{task_id}/claims/{claim_id}/revoke")
    async def revoke_claim(request: Request, task_id: str, claim_id: str,
                           body: dict = Body(default={}), project: str = Query(...)):
        project = resolve_project(project)
        body = body or {}
        principal = getattr(request.state, "principal", None)
        actor_name = auth.actor(principal) if principal else "switchboard/operator"
        sort_order = body.get("sort_order")
        try:
            sort_order_value = int(sort_order) if sort_order not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "sort_order must be an integer")
        result = store.revoke_claim(
            claim_id,
            reason=body.get("reason") or "operator override",
            reassign_to=body.get("reassign_to") or body.get("reassigned_to") or "",
            sort_order=sort_order_value,
            partial_evidence=body.get("partial_evidence") or body.get("evidence") or {},
            notify=body.get("notify") is not False,
            ack_deadline_minutes=float(body.get("ack_deadline_minutes") or 5),
            expected_task_id=task_id,
            actor=actor_name,
            project=project,
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/api/tasks/{task_id}/comment")
    async def comment(request: Request, task_id: str, body: dict = Body(...),
                      project: str = Query(...)):
        project = resolve_project(project)
        body = dict(body or {})
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        binding = resolve_write_actor(request, project, body, task_id=task_id)
        record_write_binding(task_id, binding, project)
        task = store.add_comment(task_id, binding["actor"], text, project=project)
        if not task:
            raise HTTPException(404, "task not found")
        return task

    @router.post("/api/tasks/{task_id}/dispatch")
    async def dispatch_task(task_id: str, body: dict = Body(default={})):
        project = resolve_project((body or {}).get("project") or store.DEFAULT_PROJECT)
        result = await asyncio.to_thread(
            dispatch.dispatch, task_id, (body or {}).get("actor", "user"), project,
            (body or {}).get("runtime") or "claude-code")
        if result.get("error") == "task not found":
            raise HTTPException(404, "task not found")
        return result

    @router.get("/api/tasks/{task_id}/dispatch/latest")
    async def task_dispatch_latest(task_id: str,
                                   project: str = Query(store.DEFAULT_PROJECT)):
        return await asyncio.to_thread(
            dispatch.latest, task_id, resolve_project(project))

    @router.post("/api/tasks/{task_id}/chat")
    async def chat(task_id: str, body: dict = Body(...),
                   project: str = Query(store.DEFAULT_PROJECT)):
        project = resolve_project(project)
        assistant = {"helm": "Helm", "switchboard": "Switchboard"}.get(project, "Maxwell")
        task = store.get_task(task_id, project=project)
        if not task:
            raise HTTPException(404, "task not found")
        message = (body.get("message") or "").strip()
        if not message:
            raise HTTPException(400, "message required")
        history = []
        for activity in task.get("activity", []):
            if activity.get("kind") == "chat":
                text = (activity.get("payload") or {}).get("text", "")
                if text:
                    history.append({
                        "role": "user" if activity.get("actor") == "user" else "assistant",
                        "content": text,
                    })
        history = history[-8:]
        store.add_comment(task_id, "user", message, kind="chat", project=project)
        try:
            result = await asyncio.to_thread(
                agent.run, task, message, history, project=project)
        except Exception as exc:
            store.add_comment(
                task_id, assistant, f"(agent error: {exc})", kind="chat", project=project)
            raise HTTPException(502, f"agent error: {exc}")
        answer = result.get("answer") or ""
        store.add_comment(task_id, assistant, answer, kind="chat", project=project)
        return {"answer": answer, "proposal": result.get("proposal"),
                "sources": result.get("sources", [])}

    return router
