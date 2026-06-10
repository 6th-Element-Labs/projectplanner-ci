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
import export  # noqa: E402
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
