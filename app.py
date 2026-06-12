#!/usr/bin/env python3
"""taikun-pm — opt-in project-board satellite microservice (see ADR 0007).

Standalone FastAPI app (port 8110). Owns: the board UI (static/), task state
(SQLite via store.py), and live exports (export.py). Borrows only the shared
LLM gateway (later, for the per-task agent). Does NOT import actionengine core
and does NOT touch the shared Postgres.

Run:
    uvicorn app:app --host 0.0.0.0 --port 8110            # from services/taikun-pm/
    python -m uvicorn app:app --port 8110
"""
import asyncio
import os
from pathlib import Path

# Load a local .env if present (SMTP/gateway config for later slices). No core import.
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from fastapi import Body, FastAPI, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import agent  # noqa: E402
import digest  # noqa: E402
import dispatch  # noqa: E402
import export  # noqa: E402
import inbox as inbox_mod  # noqa: E402
import intake  # noqa: E402
import notify  # noqa: E402
import signals  # noqa: E402
import store  # noqa: E402

app = FastAPI(title="Taikun PM", version="0.1.0")

store.init_db()
_seeded = store.seed_if_empty()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "taikun-pm", "tasks": len(store.list_tasks())}


@app.get("/api/board")
async def board():
    return store.board_payload()


@app.get("/api/people")
async def people():
    return {"people": store.get_meta("people", store.DEFAULT_PEOPLE)}


@app.get("/api/tasks")
async def list_tasks(workstream: str = None, status: str = None, assignee: str = None):
    return {"tasks": store.list_tasks(workstream, status, assignee)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.post("/api/tasks")
async def create_task(body: dict = Body(...)):
    actor = body.pop("_actor", "user")
    t = store.create_task(body, actor=actor)
    if not t:
        raise HTTPException(400, "workstream_id and title are required")
    return t


@app.patch("/api/tasks/{task_id}")
async def patch_task(task_id: str, body: dict = Body(...)):
    actor = body.pop("_actor", "user")
    t = store.update_task(task_id, body, actor=actor)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    if not store.delete_task(task_id):
        raise HTTPException(404, "task not found")
    return {"deleted": task_id}


@app.post("/api/tasks/{task_id}/comment")
async def comment(task_id: str, body: dict = Body(...)):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    t = store.add_comment(task_id, body.get("actor", "user"), text)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.get("/api/dispatch/status")
async def dispatch_status():
    """Is Claude Code dispatch wired (PM_CC_ROUTINE_URL + token set)?"""
    return dispatch.status()


@app.post("/api/tasks/{task_id}/dispatch")
async def dispatch_task(task_id: str, body: dict = Body(default={})):
    """Push this task to the Claude Code runner (→ claude/ branch + PR). The human-triggered (A) entry."""
    res = await asyncio.to_thread(dispatch.dispatch, task_id, (body or {}).get("actor", "user"))
    if res.get("error") == "task not found":
        raise HTTPException(404, "task not found")
    return res


@app.get("/api/dispatch/job/{job_id}")
async def dispatch_job(job_id: str):
    """Status of a dispatched runner job (running|pushed|no_changes|…) + PR url + log tail."""
    return await asyncio.to_thread(dispatch.job_status, job_id)


@app.get("/api/tasks/{task_id}/dispatch/latest")
async def task_dispatch_latest(task_id: str):
    """The latest Claude Code dev run for a task: status + PR url + full run log (for the UI panel)."""
    d = store.latest_dispatch(task_id)
    if not d:
        return {"job_id": None}
    js = await asyncio.to_thread(dispatch.job_status, d["job_id"])
    return {"job_id": d["job_id"], "created_at": d.get("created_at"),
            **(js if isinstance(js, dict) else {})}


@app.post("/api/tasks/{task_id}/chat")
async def chat(task_id: str, body: dict = Body(...)):
    """Per-task Ask Taikun agent: RAG over the plan docs + propose-then-confirm task edits."""
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message required")
    history = []
    for a in task.get("activity", []):
        if a.get("kind") == "chat":
            text = (a.get("payload") or {}).get("text", "")
            if text:
                history.append({"role": "user" if a.get("actor") == "user" else "assistant", "content": text})
    history = history[-8:]
    store.add_comment(task_id, "user", msg, kind="chat")
    try:
        result = await asyncio.to_thread(agent.run, task, msg, history)
    except Exception as e:
        store.add_comment(task_id, "Maxwell", f"(agent error: {e})", kind="chat")
        raise HTTPException(502, f"agent error: {e}")
    answer = result.get("answer") or ""
    store.add_comment(task_id, "Maxwell", answer, kind="chat")
    return {"answer": answer, "proposal": result.get("proposal"), "sources": result.get("sources", [])}


@app.post("/api/chat")
async def plan_chat(body: dict = Body(...)):
    """Plan-wide Ask Taikun: the global agent sees the whole board + docs; propose-to-confirm."""
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message required")
    session = body.get("session") or "plan"
    history = [{"role": m["role"], "content": m["content"]}
               for m in store.recent_chat(session, 16) if m.get("content")]
    store.add_chat(session, "user", msg)
    try:
        result = await asyncio.to_thread(agent.run, None, msg, history)
    except Exception as e:
        store.add_chat(session, "assistant", f"(agent error: {e})")
        raise HTTPException(502, f"agent error: {e}")
    answer = result.get("answer") or ""
    store.add_chat(session, "assistant", answer,
                   {"proposals": result.get("proposals", []), "sources": result.get("sources", [])})
    return {"answer": answer, "proposal": result.get("proposal"),
            "proposals": result.get("proposals", []), "sources": result.get("sources", [])}


@app.get("/api/chat/history")
async def plan_chat_history(session: str = "plan"):
    return {"messages": store.recent_chat(session, 100)}


@app.delete("/api/chat")
async def clear_plan_chat(session: str = "plan"):
    store.clear_chat(session)
    return {"cleared": session}


@app.post("/api/intake")
async def intake_artifact(body: dict = Body(...)):
    """Ingest an artifact (transcript/email/document) into RAG + triage it against the plan.
    Returns {summary, proposals, new_tasks, sources, ingested_chunks} — propose-to-confirm."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        return await asyncio.to_thread(
            intake.ingest_and_triage, body.get("kind") or "note", body.get("title") or "", text)
    except Exception as e:
        raise HTTPException(502, f"intake error: {e}")


# ---- Live Inbox (Phase 5.5) -------------------------------------------------
@app.get("/api/inbox")
async def get_inbox(status: str = None):
    return {"items": store.list_inbox(status), "pending": store.inbox_pending_count()}


@app.post("/api/inbox/{item_id}/confirm")
async def confirm_inbox(item_id: int, body: dict = Body(default={})):
    item = store.get_inbox_item(item_id)
    if not item:
        raise HTTPException(404, "no such inbox item")
    tri = item.get("triage") or {}
    applied = inbox_mod.apply(body.get("proposals", tri.get("proposals", [])),
                              body.get("new_tasks", tri.get("new_tasks", [])))
    store.set_inbox_status(item_id, "confirmed")
    return {"applied": applied}


@app.post("/api/inbox/{item_id}/dismiss")
async def dismiss_inbox(item_id: int):
    if not store.get_inbox_item(item_id):
        raise HTTPException(404, "no such inbox item")
    store.set_inbox_status(item_id, "dismissed")
    return {"dismissed": item_id}


@app.post("/api/inbox/simulate")
async def simulate_inbox(body: dict = Body(...)):
    """Inject a fake inbound email to exercise the Live Inbox pipeline without a mailbox."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    sender = body.get("sender") or "tester@taikunai.com"
    headers = {"from": sender, "to": body.get("to") or "", "cc": body.get("cc") or "",
               "date": body.get("date") or "", "message_id": body.get("message_id") or ""}
    try:
        item = await asyncio.to_thread(
            inbox_mod.process, "email-sim", "sim-" + os.urandom(6).hex(),
            sender, body.get("subject") or "(simulated)", text, headers)
    except Exception as e:
        raise HTTPException(502, f"inbox error: {e}")
    return item or {"deduped": True}


@app.post("/api/inbox/poll")
async def poll_inbox_now():
    import gmail_source
    return await asyncio.to_thread(gmail_source.poll)


@app.get("/api/signals")
async def plan_signals():
    """Derived plan health: overdue / due-soon / blocked / ready / critical-slip /
    past-due decisions + each owner's next-best 1-2 tasks."""
    return signals.compute_plan_signals()


@app.post("/api/digest")
async def make_digest():
    """Generate + post the weekly chief-of-staff brief (signals + activity deltas)."""
    try:
        return await asyncio.to_thread(digest.generate_digest)
    except Exception as e:
        raise HTTPException(502, f"digest error: {e}")


@app.get("/api/digests")
async def get_digests():
    return {"digests": store.list_digests(20)}


@app.get("/api/notify/status")
async def notify_status():
    """Which channels are wired (configured) vs dry-run."""
    return notify.status()


@app.post("/api/notify/test")
async def notify_test():
    return {"results": notify.send("Project Maxwell — test", "Notify is wired (test message from plan.taikunai.com).")}


@app.post("/api/digest/{digest_id}/send")
async def send_digest(digest_id: int):
    d = next((x for x in store.list_digests(50) if x["id"] == digest_id), None)
    if not d:
        raise HTTPException(404, "no such digest")
    proj = store.get_meta("project") or "the plan"
    return {"results": await asyncio.to_thread(notify.send, f"{proj} — digest", d["content"])}


def _people_of(t, people):
    """Owner-person(s) for a task — match the people list against owner_person_or_role.
    Mirrors the board UI's _peopleOf so 'export = what you see' for the owner filter."""
    owner = (t.get("owner_person_or_role") or "").lower()
    if not owner:
        return ["Unassigned"]
    m = [p for p in people if p.lower() in owner]
    return m or ["Unassigned"]


def _filtered_payload(workstream=None, owner=None, risk=None, blocking=0, q=None, person=None):
    """Same filter semantics as the board UI, so 'export = what you see'."""
    p = store.board_payload()
    ql = (q or "").lower()
    people = store.get_meta("people", store.DEFAULT_PEOPLE) if person else []

    def keep(t):
        if workstream and t.get("_wsId") != workstream:
            return False
        if owner and t.get("owner_org") != owner:
            return False
        if person and person not in _people_of(t, people):
            return False
        if risk and t.get("risk_level") != risk:
            return False
        if blocking and not t.get("is_blocking"):
            return False
        if ql:
            hay = f"{t.get('task_id','')} {t.get('title','')} {t.get('description','')} {t.get('owner_person_or_role','')} {t.get('_wsName','')}".lower()
            if ql not in hay:
                return False
        return True

    p["workstreams"] = [{**w, "tasks": [t for t in w["tasks"] if keep(t)]} for w in p["workstreams"]]
    p["workstreams"] = [w for w in p["workstreams"] if w["tasks"]]
    return p


@app.get("/api/export.xlsx")
async def export_xlsx(workstream: str = None, owner: str = None, risk: str = None, blocking: int = 0, q: str = None, person: str = None):
    data = export.export_xlsx(_filtered_payload(workstream, owner, risk, blocking, q, person))
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="project-plan.xlsx"'})


@app.get("/api/export.xml")
async def export_xml(workstream: str = None, owner: str = None, risk: str = None, blocking: int = 0, q: str = None, person: str = None):
    xml = export.export_mspdi(_filtered_payload(workstream, owner, risk, blocking, q, person))
    return Response(content=xml, media_type="text/xml",
                    headers={"Content-Disposition": 'attachment; filename="project-plan.xml"'})


# Static board UI last, so /api/* and /health win. html=True serves index.html at /.
_static = Path(__file__).parent / "static"
if _static.exists():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PM_PORT", "8110")))
