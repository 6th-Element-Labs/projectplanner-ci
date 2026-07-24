"""Universal attention ingress for operators and Agent Hosts.

The legacy ``GET /api/attention`` unions stores that hold human-blocking work
into one normalized, ranked feed.  The feed is a projection only: every item
keeps a stable pointer to its authoritative source and all decisions continue
to route to that source's write API.

  * ``agent_messages``  — unacked required messages (an agent is parked on you)
  * ``inbox``           — pending triaged inbound (plan@taikunai.com, uploads)

That legacy feed remains read-only: deciding an item routes to endpoints that own
each store's writes (``/api/agent_messages/ack``, ``/api/inbox/{id}/confirm`` /
``/dismiss``).

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
from switchboard.storage.repositories.attention import (
    AttentionStoreError,
    COMPLETION_CLOSEOUT_SCHEMA,
    COMPLETION_PROVIDER,
)

ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]
PendingAcksFn = Callable[..., List[Dict[str, Any]]]
ListInboxFn = Callable[..., List[Dict[str, Any]]]
ListDeliverablesFn = Callable[..., List[Dict[str, Any]]]
GetMissionStatusFn = Callable[..., Dict[str, Any]]
ListDecisionsFn = Callable[..., List[Dict[str, Any]]]
DecisionRecordedFn = Callable[[Dict[str, Any], str, str], Dict[str, Any]]

ATTENTION_PROJECTION_SCHEMA = "switchboard.attention_projection.v1"
_IMPACT_RANK = {"blocking": 0, "at_risk": 1, "none": 2}


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
    auto_proceed: bool = False


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
    runner_session_id: str = ""
    work_session_id: str = ""


class AttentionDeliveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1)
    host_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1)
    receipt: Any = None
    error: str = ""
    provider: str = ""
    runner_session_id: str = ""
    work_session_id: str = ""


def _age_s(ts: Any) -> int:
    try:
        return max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return 0


def _agent_item(msg: Dict[str, Any]) -> Dict[str, Any]:
    """An unacked required agent message — someone's session is parked on you."""
    return {
        "attention_id": f"message:{msg.get('id')}",
        "source_id": f"message:{msg.get('id')}",
        "source": "agent",
        "kind": "agent_message",
        "task_id": msg.get("task_id") or "",
        "title": (msg.get("message") or "")[:120],
        "summary": msg.get("message") or "",
        "from": msg.get("from_agent") or "",
        "to": msg.get("to_agent") or "",
        "age_s": _age_s(msg.get("sent_at")),
        "deadline": msg.get("ack_deadline"),
        "delivery_impact": "blocking",
        "unfinished_downstream": int(msg.get("unfinished_downstream") or 0),
        "links": {
            "task": f"#task/{msg.get('task_id')}" if msg.get("task_id") else None,
            "provider": None, "host": msg.get("host_id"),
            "session": msg.get("runner_session_id") or msg.get("work_session_id"),
        },
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
        "source_id": f"inbox:{it.get('id')}",
        "source": "inbox",
        "kind": (it.get("source") or "email"),
        "task_id": touched[0] if touched else "",
        "title": it.get("subject") or (it.get("source") or "inbound"),
        "summary": it.get("summary") or "",
        "from": it.get("sender") or "",
        "age_s": _age_s(it.get("received_at")),
        "deadline": None,
        "delivery_impact": "at_risk" if touched else "none",
        "unfinished_downstream": len(set(touched)),
        "links": {"task": f"#task/{touched[0]}" if touched else None,
                  "provider": None, "host": None, "session": None},
        "payload": {"inbox_id": it.get("id"),
                    "proposals": len(proposals), "new_tasks": len(new_tasks),
                    "touches": touched[:6],
                    "triage_error": tri.get("triage_error") or None},
        "decide": {"method": "POST", "path": f"/api/inbox/{it.get('id')}/confirm",
                   "alt": f"/api/inbox/{it.get('id')}/dismiss"},
    }


def _provider_item(item: Dict[str, Any]) -> Dict[str, Any]:
    request_id = str(item.get("request_id") or "")
    task_id = str(item.get("task_id") or "")
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    provider = str(item.get("provider") or "")
    recommended = item.get("recommended_default")
    completion_human = (
        provider == COMPLETION_PROVIDER
        and str(item.get("schema_version") or "") == COMPLETION_CLOSEOUT_SCHEMA
    )
    kind = "completion_human" if completion_human else "provider_request"
    status = str(item.get("status") or "pending")
    return {
        "attention_id": f"provider:{request_id}", "source_id": f"provider:{request_id}",
        "source": "provider", "kind": kind, "task_id": task_id,
        "title": str(item.get("prompt") or "")[:120],
        "summary": str(item.get("prompt") or ""), "from": provider,
        "to": "", "age_s": _age_s(item.get("created_at")),
        "deadline": item.get("expires_at"), "delivery_impact": "blocking",
        "unfinished_downstream": int(context.get("unfinished_downstream") or 0),
        "payload": {
            "request_id": request_id, "version": item.get("version"),
            "schema_version": item.get("schema_version"),
            "provider": provider, "status": status,
            "choices": item.get("choices"), "recommended_default": recommended,
            "reason_code": context.get("reason_code") or context.get("unresolved_gate"),
            "pr_number": context.get("pr_number"),
            "head_sha": context.get("head_sha"),
            "deliverable_id": context.get("deliverable_id"),
            "completed_work_summary": context.get("completed_work_summary"),
            "why_automation_stopped": context.get("why_automation_stopped"),
            "what_you_need_to_do": context.get("what_you_need_to_do"),
            "resume_condition": context.get("resume_condition"),
            "next_automatic_action": context.get("next_automatic_action"),
            "evidence": context.get("evidence") or context.get("evidence_refs"),
            "blast_radius": context.get("blast_radius"),
            "frozen_payload": context,
            "delivery_receipt": item.get("delivery_receipt"),
            "completion_wake": item.get("completion_wake"),
            "terminal_reason": item.get("terminal_reason"),
        },
        "links": {
            "mission": context.get("mission_id") or context.get("deliverable_id"),
            "deliverable": context.get("deliverable_id"),
            "task": f"#task/{task_id}" if task_id else None,
            "provider": provider or None, "host": item.get("host_id"),
            "session": item.get("runner_session_id") or item.get("work_session_id"),
        },
        "decide": {
            "method": "POST",
            "path": f"/api/attention/requests/{request_id}/decide",
            "body": {
                "expected_version": item.get("version"),
                "choice": recommended,
                "idempotency_key": f"operator-decide:{request_id}",
            },
        },
    }


def _mission_item(status: Dict[str, Any], action: Dict[str, Any], index: int) -> Dict[str, Any]:
    deliverable_id = str(status.get("deliverable_id") or "")
    action_key = str(action.get("action") or index)
    task_id = str(action.get("task_id") or "")
    source_id = f"mission:{deliverable_id}:{action_key}:{task_id or index}"
    return {
        "attention_id": source_id, "source_id": source_id, "source": "mission",
        "kind": "deliverable_action", "task_id": task_id,
        "title": action.get("label") or action_key,
        "summary": action.get("reason") or "", "from": "mission", "to": "",
        "age_s": _age_s(action.get("created_at") or status.get("narrative_updated_at")),
        "deadline": action.get("deadline"),
        "delivery_impact": action.get("delivery_impact") or "none",
        "unfinished_downstream": int(action.get("unfinished_downstream") or
                                     action.get("blocking_task_count") or 0),
        "payload": {"action": action_key, "owner_type": action.get("owner_type"),
                    "evidence": action.get("evidence"),
                    "blast_radius": action.get("blast_radius")},
        "links": {"mission": status.get("mission_id") or status.get("board_id"),
                  "deliverable": deliverable_id,
                  "task": f"#task/{task_id}" if task_id else None,
                  "provider": action.get("provider"), "host": action.get("host_id"),
                  "session": action.get("runner_session_id") or action.get("work_session_id")},
        "decide": action.get("decide"),
    }


def _decision_item(item: Dict[str, Any]) -> Dict[str, Any]:
    decision_id = str(item.get("decision_id") or item.get("decision_key") or item.get("id"))
    task_id = str(item.get("task_id") or "")
    return {
        "attention_id": f"decision:{decision_id}", "source_id": f"decision:{decision_id}",
        "source": "decision", "kind": "plan_decision", "task_id": task_id,
        "title": item.get("title") or "Open plan decision",
        "summary": item.get("rationale") or item.get("context") or "", "from": "plan", "to": "",
        "age_s": _age_s(item.get("created_at")), "deadline": item.get("deadline"),
        "delivery_impact": item.get("delivery_impact") or "at_risk",
        "unfinished_downstream": int(item.get("unfinished_downstream") or 0),
        "payload": {"decision_id": decision_id, "status": item.get("status"),
                    "chosen_action": item.get("chosen_action"),
                    "evidence": item.get("evidence"), "blast_radius": item.get("blast_radius")},
        "links": {"mission": item.get("mission_id"),
                  "deliverable": item.get("deliverable_id"),
                  "task": f"#task/{task_id}" if task_id else None,
                  "provider": None, "host": None, "session": None},
        "decide": item.get("decide"),
    }


def _rank(item: Dict[str, Any]) -> tuple:
    """Delivery impact, unfinished downstream work, deadline, then oldest first."""
    deadline = item.get("deadline")
    try:
        deadline_key = float(deadline) if deadline is not None else float("inf")
    except (TypeError, ValueError):
        deadline_key = float("inf")
    return (_IMPACT_RANK.get(str(item.get("delivery_impact")), 2),
            -int(item.get("unfinished_downstream") or 0),
            deadline_key, -int(item.get("age_s") or 0),
            str(item.get("source_id") or item.get("attention_id") or ""))


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse repeated reads of one source record without merging source stores."""
    projected: Dict[str, Dict[str, Any]] = {}
    for item in items:
        source_id = str(item.get("source_id") or item.get("attention_id") or "")
        if source_id and source_id not in projected:
            projected[source_id] = item
    return list(projected.values())


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
        "attention_binding_mismatch": 403,
        "attention_principal_unbound": 403,
        "attention_completion_owner_required": 403,
        "attention_request_expired": 409,
        "stale_attention_head": 409,
        "stale_attention_pr": 409,
        "stale_attention_completion_run": 409,
        "attention_head_unverifiable": 409,
    }.get(exc.code, 400)
    raise HTTPException(status, exc.as_dict()) from exc


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver,
                  list_pending_acks: PendingAcksFn,
                  list_inbox: ListInboxFn,
                  list_deliverables: Optional[ListDeliverablesFn] = None,
                  get_mission_status: Optional[GetMissionStatusFn] = None,
                  list_decisions: Optional[ListDecisionsFn] = None,
                  on_decision_recorded: Optional[DecisionRecordedFn] = None,
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
        for it in list_inbox("pending", project=proj):
            items.append(_inbox_item(it))
        provider_queue = service.list_operator_queue(
            _context(proj, principal, source="query"), limit=500)
        items.extend(_provider_item(it) for it in provider_queue.get("items", []))
        if list_deliverables and get_mission_status:
            for deliverable in list_deliverables(
                    project=proj, include_task_snapshots=False):
                status = get_mission_status(
                    project=proj, deliverable_id=str(deliverable.get("id") or ""))
                for index, action in enumerate(status.get("next_actions") or []):
                    if action.get("attention"):
                        items.append(_mission_item(status, action, index))
        if list_decisions:
            for decision in list_decisions(project=proj, status="proposed", limit=500):
                items.append(_decision_item(decision))

        items = _dedupe(items)
        items.sort(key=_rank)
        sources = {
            source: sum(1 for item in items if item["source"] == source)
            for source in ("provider", "agent", "inbox", "mission", "decision")
        }
        return {"schema": ATTENTION_PROJECTION_SCHEMA, "project": proj,
                "count": len(items), "items": items, "sources": sources}

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
            decided = service.decide(
                _context(project_id, principal, source="query"), request_id,
                body.model_dump(), actor=auth.actor(principal))
            wake = decided.get("completion_wake")
            if (
                on_decision_recorded is not None
                and isinstance(wake, dict)
                and str(wake.get("status") or "") in {"pending", "failed"}
            ):
                try:
                    attempted = on_decision_recorded(
                        decided, project_id, auth.actor(principal))
                    if attempted:
                        decided["completion_wake"] = attempted
                        if isinstance(attempted.get("request"), dict):
                            decided["request"] = attempted["request"]
                except Exception as exc:  # the decision remains authoritative
                    # The decision and pending outbox row committed together.
                    # Preserve that authority and let the daemon retry it.
                    decided["completion_wake"] = {
                        **wake,
                        "status": "pending",
                        "last_error": f"{type(exc).__name__}: {exc}",
                        "retryable": True,
                    }
            return decided
        except AttentionStoreError as exc:
            _raise_attention_error(exc)

    @router.post("/ixp/v1/attention/requests")
    async def upsert_attention_request(
        request: Request, body: AttentionRequestBody,
    ):
        payload = body.model_dump()
        if payload.pop("auto_proceed", False):
            raise HTTPException(
                400, {"error": "attention_auto_proceed_forbidden",
                      "message": "attention requests require an explicit operator decision"})
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
                request_id=payload["request_id"],
                runner_session_id=payload["runner_session_id"],
                work_session_id=payload["work_session_id"],
                actor=auth.actor(principal))
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
                error=payload["error"], provider=payload["provider"],
                runner_session_id=payload["runner_session_id"],
                work_session_id=payload["work_session_id"])
        except AttentionStoreError as exc:
            _raise_attention_error(exc)

    return router
