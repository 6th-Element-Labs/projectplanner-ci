"""UI-29: the universal attention view — one queue of everything awaiting a human.

``GET /api/attention`` unions the stores that already hold human-blocking work
into one normalized, ranked feed:

  * ``agent_messages``  — unacked required messages (an agent is parked on you)
  * ``inbox``           — pending triaged inbound (plan@taikunai.com, uploads)

READ-ONLY by design: deciding an item routes to the endpoints that already own
each store's writes (``/api/agent_messages/ack``, ``/api/inbox/{id}/confirm`` /
``/dismiss``), so this view adds no new mutation path. Later slices can fold in
plan decisions and deliverable next-actions where ``attention=True``
(see switchboard/storage/repositories/deliverables.py) and replace the
heuristic rank with a mission_graph blast radius.

Persistence reads are injected by the composition root (``list_pending_acks``,
``list_inbox``) rather than importing the ``store`` façade — same dependency
pattern as ``resolve_project`` / ``resolve_principal``.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List

from fastapi import APIRouter, Query, Request

import auth

ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
PendingAcksFn = Callable[..., List[Dict[str, Any]]]
ListInboxFn = Callable[..., List[Dict[str, Any]]]


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


def _rank(item: Dict[str, Any]) -> tuple:
    """Heuristic until mission_graph blast radius lands: agents first (a lease is
    parked), deadline-bearing items next, then breadth of proposed change, then age."""
    src_w = 0 if item["source"] == "agent" else 1
    dl = 0 if item.get("deadline") else 1
    breadth = -(item.get("payload", {}).get("proposals") or 0)
    return (src_w, dl, breadth, -item.get("age_s", 0))


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  list_pending_acks: PendingAcksFn,
                  list_inbox: ListInboxFn) -> APIRouter:
    """Read-only attention view against the monolith's shared trust boundaries."""
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

        items.sort(key=_rank)
        return {"project": proj, "count": len(items), "items": items,
                "sources": {"agent": sum(1 for i in items if i["source"] == "agent"),
                            "inbox": sum(1 for i in items if i["source"] == "inbox")}}

    return router
