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
from switchboard.api.idempotency import (
    inject_idem_key,
    raise_if_idem_conflict,
    run_with_idempotency,
)
from switchboard.application.commands import create_task as create_task_command
from switchboard.application.commands import move_task as move_task_command
from switchboard.application.commands import review_verdicts as review_verdict_commands
from switchboard.application.commands import update_task as update_task_command
from switchboard.application.queries import get_task as get_task_query
from switchboard.application.queries import review_remediations as review_remediation_queries
from switchboard.application.queries import review_verdicts as review_verdict_queries
from switchboard.contracts.tasks.v1 import (
    CREATE_TASK_FIELDS,
    UPDATE_TASK_FIELDS,
    CreateTaskCommand,
    UpdateTaskCommand,
)


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  thin_mode_a: bool = False) -> APIRouter:
    """Build the task router against the monolith's shared trust boundaries.

    When ``thin_mode_a`` is True (Tasks process cut, ADR-0012 Mode A), omit
    sibling-BC subpaths ``…/review_*``, ``…/dispatch``, and ``…/chat`` so
    ``:8122`` does not dual-mount review/dispatch/plan-chat surfaces.
    """
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
        for key in ("agent_id", "system_actor", "system_reason", "idem_key"):
            clean.pop(key, None)
        return clean

    def create_idem_payload(body: dict, *, project: str) -> dict:
        # Hash the command-normalized shape so equivalent adapter bodies replay.
        try:
            normalized = CreateTaskCommand.from_mapping(body).to_store_data()
        except Exception:
            normalized = {key: body[key] for key in CREATE_TASK_FIELDS if key in body}
        return {"project": project, **normalized}

    def update_idem_payload(body: dict, *, project: str, task_id: str) -> dict:
        try:
            normalized = UpdateTaskCommand.from_mapping(task_id, body).to_store_fields()
        except Exception:
            normalized = {key: body[key] for key in UPDATE_TASK_FIELDS if key in body}
            if "depends_on" in body:
                normalized["depends_on"] = body["depends_on"]
        return {"project": project, "task_id": task_id, **normalized}

    def move_idem_payload(body: dict, *, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "project_from": body.get("project_from") or "",
            "project_to": body.get("project_to") or "",
            "reason": body.get("reason") or "",
            "new_task_id": body.get("new_task_id") or "",
            "dependency_policy": body.get("dependency_policy") or "fail",
        }

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

    if not thin_mode_a:
        @router.post("/api/tasks/{task_id}/review_verdict")
        async def record_review_verdict(request: Request, task_id: str,
                                        body: dict = Body(...), project: str = Query(...)):
            """Persist independent review judgment for exactly one current PR head."""
            project = resolve_project(project)
            payload = dict(body or {})
            payload["task_id"] = task_id
            reviewer = str(payload.get("reviewer_principal") or "").strip()
            binding = resolve_write_actor(
                request, project, {**payload, "agent_id": reviewer}, task_id=task_id,
                scopes=("write:ixp",),
            )
            payload["reviewer_principal"] = binding["actor"]
            result = review_verdict_commands.execute_mapping(
                payload, actor=binding["actor"],
                # The resolver is the transport's authenticated source of truth.  In
                # dev-open mode it authenticates inside resolve_write_actor() without
                # middleware populating request.state, while its returned binding still
                # carries the principal ID.  Reading from request.state here therefore
                # made legitimate dev-open reviews fail closed as falsely unbound.
                principal_id=binding.get("principal_id") or "",
                project=project,
            )
            if result.get("error_code") == "review_task_not_found":
                raise HTTPException(404, result)
            if result.get("error_code") in {
                "reviewer_principal_mismatch", "reviewer_principal_unbound",
                "reviewer_not_independent",
                "review_head_unbound", "stale_review_head", "review_pr_mismatch",
                "review_verdict_conflict",
                "adversarial_review_required",
            }:
                raise HTTPException(409, result)
            if result.get("error"):
                raise HTTPException(400, result)
            if result.get("created"):
                record_write_binding(task_id, binding, project)
            return result

        @router.get("/api/tasks/{task_id}/review_verdict")
        async def get_review_verdict(task_id: str, head_sha: str = "",
                                     project: str = Query(store.DEFAULT_PROJECT)):
            project = resolve_project(project)
            verdict = review_verdict_queries.get_for(
                task_id, project=project, head_sha=head_sha)
            if not verdict:
                raise HTTPException(404, "review verdict not found")
            return verdict

        @router.get("/api/tasks/{task_id}/review_findings")
        async def list_review_findings(
                task_id: str, head_sha: str = "", state: str = "",
                finding_class: str = Query(default="", alias="class"), severity: str = "",
                current_head_only: bool = False,
                project: str = Query(store.DEFAULT_PROJECT)):
            project = resolve_project(project)
            findings = review_verdict_queries.list_findings_for(
                task_id, project=project, head_sha=head_sha, state=state,
                finding_class=finding_class, severity=severity,
                current_head_only=current_head_only,
            )
            return {"task_id": task_id, "finding_count": len(findings),
                    "findings": findings}

        @router.post("/api/tasks/{task_id}/review_findings/{finding_id}/resolution")
        async def resolve_review_finding(
                request: Request, task_id: str, finding_id: str,
                body: dict = Body(...), project: str = Query(...)):
            """Admin-authorized, audited waiver/override for one exact-head finding."""
            project = resolve_project(project)
            payload = dict(body or {})
            payload["task_id"] = task_id
            payload["finding_id"] = finding_id
            resolver = str(payload.get("resolver_principal") or "").strip()
            binding = resolve_write_actor(
                request, project, {**payload, "agent_id": resolver}, task_id=task_id,
                scopes=("admin",),
            )
            payload["resolver_principal"] = binding["actor"]
            result = review_verdict_commands.resolve_finding_mapping(
                payload, actor=binding["actor"],
                principal_id=binding.get("principal_id") or "",
                authorized=True, project=project,
            )
            if result.get("error_code") in {
                "review_resolution_forbidden", "review_resolver_principal_mismatch",
                "review_resolver_principal_unbound",
            }:
                raise HTTPException(403, result)
            if result.get("error_code") in {
                "stale_review_head", "review_head_unbound", "review_finding_not_open",
            }:
                raise HTTPException(409, result)
            if result.get("error_code") in {
                "review_task_not_found", "review_verdict_not_found",
                "review_finding_not_found",
            }:
                raise HTTPException(404, result)
            if result.get("error"):
                raise HTTPException(400, result)
            if result.get("resolved"):
                record_write_binding(task_id, binding, project)
            return result

        @router.get("/api/tasks/{task_id}/review_remediations")
        async def list_review_remediations(task_id: str, status: str = "",
                                           project: str = Query(store.DEFAULT_PROJECT)):
            project = resolve_project(project)
            rows = review_remediation_queries.list_for(
                project=project, task_id=task_id, status=status)
            return {
                "task_id": task_id,
                "remediation_count": len(rows),
                "remediations": rows,
                "metrics": review_remediation_queries.metrics_for(
                    project=project, task_id=task_id),
            }

    @router.post("/api/tasks")
    async def create_task(request: Request, body: dict = Body(...),
                          project: str = Query(...)):
        project = resolve_project(project)
        body = inject_idem_key(request, body)
        binding = resolve_write_actor(request, project, body)
        idem_key = str(body.get("idem_key") or "").strip()
        cmd_body = without_write_binding_fields(body)
        task, replayed = run_with_idempotency(
            project=project,
            operation="create_task",
            actor=binding["actor"],
            idem_key=idem_key,
            payload=create_idem_payload(cmd_body, project=project),
            execute=lambda: create_task_command.execute_mapping_result(
                cmd_body, actor=binding["actor"], project=project),
        )
        task = raise_if_idem_conflict(task)
        if task.get("error"):
            raise HTTPException(400, task)
        if not replayed:
            record_write_binding(task.get("task_id") or "", binding, project)
        return task

    @router.patch("/api/tasks/{task_id}")
    async def patch_task(request: Request, task_id: str, body: dict = Body(...),
                         project: str = Query(...)):
        project = resolve_project(project)
        body = inject_idem_key(request, body)
        binding = resolve_write_actor(request, project, body, task_id=task_id)
        idem_key = str(body.get("idem_key") or "").strip()
        cmd_body = without_write_binding_fields(body)
        task, replayed = run_with_idempotency(
            project=project,
            operation="update_task",
            actor=binding["actor"],
            idem_key=idem_key,
            payload=update_idem_payload(cmd_body, project=project, task_id=task_id),
            execute=lambda: update_task_command.execute_mapping_result(
                task_id, cmd_body, actor=binding["actor"], project=project),
        )
        task = raise_if_idem_conflict(task)
        if not task:
            raise HTTPException(404, "task not found")
        if task.get("error") == "done_requires_merge_provenance":
            raise HTTPException(409, task.get("message") or "Done requires merge provenance")
        if task.get("error"):
            raise HTTPException(400, task)
        if not replayed:
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
        body = inject_idem_key(request, body)
        # Resolve destination through the same project gate as the query arg,
        # then hand a transport-neutral payload to the shared command.
        destination = body.get("project_to") or body.get("destination_project") or ""
        body["project_from"] = project_from
        body["project_to"] = resolve_project(destination) if destination else ""
        principal = resolve_principal(
            request, "switchboard", ("write:system",), dev_actor="web")
        actor = auth.actor(principal)
        idem_key = str(body.get("idem_key") or "").strip()
        cmd_body = {k: v for k, v in body.items() if k != "idem_key"}
        result, _replayed = run_with_idempotency(
            project=project_from,
            operation="move_task",
            actor=actor,
            idem_key=idem_key,
            payload=move_idem_payload(cmd_body, task_id=task_id),
            execute=lambda: move_task_command.execute_mapping_result(
                task_id, cmd_body, actor=actor),
        )
        result = raise_if_idem_conflict(result)
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
        body = inject_idem_key(request, body)
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        binding = resolve_write_actor(request, project, body, task_id=task_id)
        idem_key = str(body.get("idem_key") or "").strip()
        task, replayed = run_with_idempotency(
            project=project,
            operation="add_comment",
            actor=binding["actor"],
            idem_key=idem_key,
            payload={"project": project, "task_id": task_id, "text": text},
            execute=lambda: store.add_comment(
                task_id, binding["actor"], text, project=project),
        )
        task = raise_if_idem_conflict(task)
        if not task:
            raise HTTPException(404, "task not found")
        if not replayed:
            record_write_binding(task_id, binding, project)
        return task

    if not thin_mode_a:
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
