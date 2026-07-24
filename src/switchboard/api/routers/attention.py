"""Universal attention ingress for operators and Agent Hosts.

The legacy ``GET /api/attention`` unions stores that hold human-blocking work
into one normalized, ranked feed:

  * ``agent_messages``  — unacked required messages (an agent is parked on you)
  * ``attention_requests`` — first-class PROTO-7/8 Needs-you rows (completion
    human closeout and provider questions)
  * ``inbox``           — pending triaged inbound (plan@taikunai.com, uploads)

Deciding an item routes to the endpoint that owns that store's writes
(``/api/agent_messages/ack``, ``/api/attention/requests/{id}/decide``,
``/api/inbox/{id}/confirm`` / ``/dismiss``).

PROTO-8 adds project-scoped durable request, decision, claim, and delivery
contracts below it. Those handlers are thin adapters over ``AttentionService``;
provider-specific behavior belongs outside this router.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

import auth
from switchboard.api.deps import (
    require_agent_host_identity,
    resolve_agent_host_principal,
)
from switchboard.application.attention import (
    AttentionService,
    default_attention_service,
)
from switchboard.domain.projects.context import ProjectContext
from switchboard.storage.repositories.attention import AttentionStoreError

ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]
PendingAcksFn = Callable[..., List[Dict[str, Any]]]
ListInboxFn = Callable[..., List[Dict[str, Any]]]


class AttentionRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_request_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    choices: list[Any] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    host_id: str = Field(min_length=1)
    task_id: Optional[str] = None
    runner_session_id: Optional[str] = None
    work_session_id: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)
    recommended_default: Any = None
    expires_at: Optional[float] = None


class AttentionDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    choice: Any
    idempotency_key: str = Field(min_length=1)


class AttentionClaimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1)
    host_id: str = Field(min_length=1)
    provider: str = ""
    request_id: str = ""


class AttentionDeliveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1)
    host_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1)
    receipt: Any = None
    error: str = ""


def _age_s(ts: Any) -> int:
    try:
        return max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return 0


def _agent_item(msg: Dict[str, Any]) -> Dict[str, Any]:
    """An unacked required agent message — someone's session is parked on you."""
    return {
        "attention_id": f"msg:{msg.get('id')}",
        "source": "agent",
        "kind": "agent_message",
        "task_id": msg.get("task_id") or "",
        "title": (msg.get("message") or "")[:120],
        "summary": msg.get("message") or "",
        "from": msg.get("from_agent") or "",
        "to": msg.get("to_agent") or "",
        "age_s": _age_s(msg.get("sent_at")),
        "deadline": msg.get("ack_deadline"),
        "payload": {"message_id": msg.get("id"),
                    "requires_ack": bool(msg.get("requires_ack")),
                    "monitor": (msg.get("monitor") or {}).get("status") if isinstance(msg.get("monitor"), dict) else None},
        # the write path that resolves this item (already exists today)
        "decide": {"method": "POST", "path": "/api/agent_messages/ack",
                   "body": {"message_id": msg.get("id"), "response": "<your answer>"}},
    }


def _inbox_item(it: Dict[str, Any]) -> Dict[str, Any]:
    """A pending triaged inbound item (email / upload / note) awaiting confirm."""
    tri = it.get("triage") or {}
    proposals = tri.get("proposals") or []
    new_tasks = tri.get("new_tasks") or []
    touched = [str(p.get("task_id") or "") for p in proposals if p.get("task_id")]
    return {
        "attention_id": f"inbox:{it.get('id')}",
        "source": "inbox",
        "kind": (it.get("source") or "email"),
        "task_id": touched[0] if touched else "",
        "title": it.get("subject") or (it.get("source") or "inbound"),
        "summary": it.get("summary") or "",
        "from": it.get("sender") or "",
        "age_s": _age_s(it.get("received_at")),
        "deadline": None,
        "payload": {"inbox_id": it.get("id"),
                    "proposals": len(proposals), "new_tasks": len(new_tasks),
                    "touches": touched[:6],
                    "triage_error": tri.get("triage_error") or None},
        "decide": {"method": "POST", "path": f"/api/inbox/{it.get('id')}/confirm",
                   "alt": f"/api/inbox/{it.get('id')}/dismiss"},
    }


def _request_item(req: Dict[str, Any]) -> Dict[str, Any]:
    """A first-class PROTO-7/8 attention_request awaiting an operator decision."""
    context = req.get("context") if isinstance(req.get("context"), dict) else {}
    request_id = str(req.get("request_id") or "")
    recommended = req.get("recommended_default")
    return {
        "attention_id": f"attention:{request_id}",
        "source": "attention",
        "kind": "completion_human" if str(req.get("provider") or "").startswith(
            "switchboard.completion") else "attention_request",
        "task_id": req.get("task_id") or context.get("task_id") or "",
        "title": (req.get("prompt") or "")[:120],
        "summary": req.get("prompt") or "",
        "from": req.get("provider") or "completion",
        "age_s": _age_s(req.get("created_at")),
        "deadline": req.get("expires_at"),
        "payload": {
            "request_id": request_id,
            "version": req.get("version"),
            "reason_code": context.get("reason_code") or context.get("unresolved_gate"),
            "pr_number": context.get("pr_number"),
            "head_sha": context.get("head_sha"),
            "resume_condition": context.get("resume_condition"),
            "next_automatic_action": context.get("next_automatic_action"),
            "choices": req.get("choices") or [],
            "recommended_default": recommended,
        },
        "decide": {
            "method": "POST",
            "path": f"/api/attention/requests/{request_id}/decide",
            "body": {
                "expected_version": req.get("version"),
                "choice": recommended,
                "idempotency_key": f"operator-decide:{request_id}",
            },
        },
    }


def _rank(item: Dict[str, Any]) -> tuple:
    """Heuristic until mission_graph blast radius lands: agents first (a lease is
    parked), durable completion Needs-you next, then inbox breadth, then age."""
    src_w = {"agent": 0, "attention": 1, "inbox": 2}.get(item.get("source"), 3)
    dl = 0 if item.get("deadline") else 1
    breadth = -(item.get("payload", {}).get("proposals") or 0)
    return (src_w, dl, breadth, -item.get("age_s", 0))


def _context(project: str, principal: dict, *, source: str) -> ProjectContext:
    scopes = principal.get("effective_scopes") or principal.get("scopes") or []
    return ProjectContext(
        project_id=project,
        source=source,
        principal_id=str(principal.get("id") or ""),
        principal_kind=str(principal.get("kind") or ""),
        principal_binding=str(principal.get("project") or ""),
        principal_display_name=str(principal.get("display_name") or ""),
        effective_scopes=tuple(sorted(str(scope) for scope in scopes)),
    )


def _raise_attention_error(exc: AttentionStoreError) -> None:
    status = {
        "attention_request_not_found": 404,
        "stale_attention_decision": 409,
        "stale_attention_version": 409,
        "attention_idempotency_conflict": 409,
        "attention_decision_idempotency_conflict": 409,
        "attention_provider_request_conflict": 409,
        "attention_host_mismatch": 403,
    }.get(exc.code, 400)
    raise HTTPException(status, exc.as_dict()) from exc


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver,
                  list_pending_acks: PendingAcksFn,
                  list_inbox: ListInboxFn,
                  service: AttentionService = default_attention_service) -> APIRouter:
    """Mount legacy feed plus durable operator and Agent Host attention contracts."""
    router = APIRouter()

    @router.get("/api/attention")
    async def api_attention(request: Request, project: str = Query(...),
                            agent_id: str = ""):
        proj = resolve_project(project)
        principal = resolve_principal(request, proj, ("read",), dev_actor="web")
        me = agent_id or auth.actor(principal)

        items: List[Dict[str, Any]] = []
        for msg in list_pending_acks(agent_id=me, project=proj):
            items.append(_agent_item(msg))
        for req in service.list_operator_queue(
                _context(proj, principal, source="query")).get("items") or []:
            items.append(_request_item(req))
        for it in list_inbox("pending", project=proj):
            items.append(_inbox_item(it))

        items.sort(key=_rank)
        return {"project": proj, "count": len(items), "items": items,
                "sources": {"agent": sum(1 for i in items if i["source"] == "agent"),
                            "attention": sum(1 for i in items if i["source"] == "attention"),
                            "inbox": sum(1 for i in items if i["source"] == "inbox")}}

    @router.get("/api/attention/requests")
    async def list_attention_requests(
        request: Request, project: str = Query(...),
        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    ):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("read",), dev_actor="attention-operator")
        return service.list_operator_queue(
            _context(project_id, principal, source="query"),
            limit=limit, offset=offset)

    @router.get("/api/attention/count")
    async def count_attention_requests(request: Request, project: str = Query(...)):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("read",), dev_actor="attention-operator")
        return service.count_operator_queue(
            _context(project_id, principal, source="query"))

    @router.get("/api/attention/requests/{request_id}")
    async def get_attention_request(
        request_id: str, request: Request, project: str = Query(...),
    ):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("read",), dev_actor="attention-operator")
        try:
            return service.get_request(
                _context(project_id, principal, source="query"), request_id)
        except AttentionStoreError as exc:
            _raise_attention_error(exc)

    @router.post("/api/attention/requests/{request_id}/decide")
    async def decide_attention_request(
        request_id: str, request: Request, body: AttentionDecisionBody,
        project: str = Query(...),
    ):
        project_id = resolve_project(project)
        principal = resolve_principal(
            request, project_id, ("write:ixp",), dev_actor="attention-operator")
        try:
            return service.decide(
                _context(project_id, principal, source="query"), request_id,
                body.model_dump(), actor=auth.actor(principal))
        except AttentionStoreError as exc:
            _raise_attention_error(exc)

    @router.post("/ixp/v1/attention/requests")
    async def upsert_attention_request(
        request: Request, body: AttentionRequestBody,
    ):
        payload = body.model_dump()
        project_id = resolve_body_project(payload)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project_id,
            dev_actor=payload["host_id"])
        require_agent_host_identity(principal, payload["host_id"], project_id)
        payload.pop("project", None)
        try:
            return service.upsert_request(
                _context(project_id, principal, source="body"), payload,
                actor=auth.actor(principal))
        except AttentionStoreError as exc:
            _raise_attention_error(exc)

    @router.post("/ixp/v1/attention/decisions/claim")
    async def claim_attention_decision(
        request: Request, body: AttentionClaimBody,
    ):
        payload = body.model_dump()
        project_id = resolve_body_project(payload)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project_id,
            dev_actor=payload["host_id"])
        require_agent_host_identity(principal, payload["host_id"], project_id)
        try:
            claimed = service.claim_decision(
                _context(project_id, principal, source="body"),
                host_id=payload["host_id"], provider=payload["provider"],
                request_id=payload["request_id"], actor=auth.actor(principal))
        except AttentionStoreError as exc:
            _raise_attention_error(exc)
        return {"claimed": claimed is not None, "delivery": claimed}

    @router.post("/ixp/v1/attention/requests/{request_id}/delivery")
    async def acknowledge_attention_delivery(
        request_id: str, request: Request, body: AttentionDeliveryBody,
    ):
        payload = body.model_dump()
        project_id = resolve_body_project(payload)
        principal = resolve_agent_host_principal(
            resolve_principal, request, project_id,
            dev_actor=payload["host_id"])
        require_agent_host_identity(principal, payload["host_id"], project_id)
        try:
            return service.acknowledge_delivery(
                _context(project_id, principal, source="body"), request_id,
                expected_version=payload["expected_version"],
                host_id=payload["host_id"], actor=auth.actor(principal),
                receipt=payload["receipt"],
                error=payload["error"])
        except AttentionStoreError as exc:
            _raise_attention_error(exc)

    return router
