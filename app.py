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

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import agent  # noqa: E402
import attachments  # noqa: E402
import digest  # noqa: E402
import transcribe  # noqa: E402
import dispatch  # noqa: E402
import export  # noqa: E402
import inbox as inbox_mod  # noqa: E402
import intake  # noqa: E402
import notify  # noqa: E402
import ocr  # noqa: E402
import rebrand  # noqa: E402
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


def _queue_triage(res, source, subject):
    """Persist a triage result into the Action Queue (Inbox) as a pending item so its proposed
    changes survive reload and are bulk-confirmable in one place — not just ephemeral chat cards.
    Only queues when there's something to act on. Mutates + returns `res` with inbox_id."""
    try:
        if res and ((res.get("proposals")) or (res.get("new_tasks"))):
            triage = {"proposals": res.get("proposals", []), "new_tasks": res.get("new_tasks", []),
                      "sources": res.get("sources", []), "summary": res.get("summary", "")}
            res["inbox_id"] = store.add_inbox_item(
                source, source + "-" + os.urandom(6).hex(), "", subject or source,
                res.get("summary", ""), triage)
    except Exception:
        pass  # queueing is best-effort; the chat cards still work
    return res


@app.post("/api/intake")
async def intake_artifact(body: dict = Body(...)):
    """Ingest an artifact (transcript/email/document) into RAG + triage it against the plan.
    Returns {summary, proposals, new_tasks, sources, ingested_chunks, inbox_id} — propose-to-confirm."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        res = await asyncio.to_thread(
            intake.ingest_and_triage, body.get("kind") or "note", body.get("title") or "", text)
        return _queue_triage(res, body.get("kind") or "note", body.get("title") or "")
    except Exception as e:
        raise HTTPException(502, f"intake error: {e}")


@app.post("/api/intake/upload")
async def intake_upload(file: UploadFile = File(...), kind: str = Form("document"),
                        title: str = Form("")):
    """Drop a file — audio/video, pdf, docx, or text — extract or TRANSCRIBE it, then
    ingest into the corpus + triage. Media is transcribed via OpenAI (Whisper) through the
    gateway; everything else uses attachments.extract. Same response shape as /api/intake."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    fn = file.filename or "upload"
    label = (title or fn).strip()
    media = transcribe.is_media(fn, file.content_type)
    try:
        if media:
            text = await asyncio.to_thread(transcribe.transcribe, fn, data, file.content_type)
        else:
            text = await asyncio.to_thread(attachments.extract, fn, file.content_type, data)
    except ValueError as e:                       # size limit etc. — user-facing
        raise HTTPException(413, str(e))
    except Exception as e:
        raise HTTPException(502, f"{'transcription' if media else 'extract'} error: {e}")
    if not text or not text.strip():
        raise HTTPException(422, f"could not get text from {fn} (unsupported type or empty)")
    try:
        res = await asyncio.to_thread(intake.ingest_and_triage, kind or "document", label, text)
    except Exception as e:
        raise HTTPException(502, f"intake error: {e}")
    res["transcribed"] = media
    res["chars"] = len(text)
    return _queue_triage(res, "transcript" if media else "upload", label)


# ---- Live Inbox (Phase 5.5) -------------------------------------------------
@app.get("/api/inbox")
async def get_inbox(status: str = None):
    return {"items": store.list_inbox(status), "pending": store.inbox_pending_count()}


@app.post("/api/inbox/{item_id}/confirm")
async def confirm_inbox(item_id: int, body: dict = Body(default={})):
    """Apply the given proposals/new_tasks (default: all of the item's). `keep_proposals` /
    `keep_new_tasks` are held back and the item STAYS pending with just those (used to bulk-
    confirm the safe changes while holding status->Done items that still need evidence).
    Edited proposals are honored — the client sends the modified field values to apply."""
    item = store.get_inbox_item(item_id)
    if not item:
        raise HTTPException(404, "no such inbox item")
    tri = item.get("triage") or {}
    applied = inbox_mod.apply(body.get("proposals", tri.get("proposals", [])),
                              body.get("new_tasks", tri.get("new_tasks", [])))
    keep_p = body.get("keep_proposals") or []
    keep_n = body.get("keep_new_tasks") or []
    tri["applied"] = applied
    if keep_p or keep_n:
        tri["proposals"], tri["new_tasks"] = keep_p, keep_n
        store.update_inbox_triage(item_id, tri)          # stays pending with the held items
    else:
        store.update_inbox_triage(item_id, tri)
        store.set_inbox_status(item_id, "confirmed")
    return {"applied": applied, "remaining": len(keep_p) + len(keep_n)}


@app.post("/api/inbox/confirm_all")
async def confirm_all_inbox(body: dict = Body(default={})):
    """Bulk-confirm pending queue items. safe_only=True applies everything EXCEPT status->Done
    proposals (which need acceptance evidence), holding those back so the item stays pending."""
    safe_only = bool(body.get("safe_only"))
    ids = body.get("ids")
    items = store.list_inbox("pending", limit=500)
    if ids:
        idset = set(ids)
        items = [it for it in items if it["id"] in idset]
    tot = {"items": 0, "updated": 0, "created": 0, "held": 0}
    for it in items:
        tri = it.get("triage") or {}
        props = tri.get("proposals", []) or []
        nts = tri.get("new_tasks", []) or []
        if safe_only:
            apply_p = [p for p in props if (p.get("status") or "") != "Done"]
            keep_p = [p for p in props if (p.get("status") or "") == "Done"]
        else:
            apply_p, keep_p = props, []
        if not (apply_p or nts):
            continue
        applied = inbox_mod.apply(apply_p, nts)
        tri["applied"] = applied
        tot["items"] += 1
        tot["updated"] += len(applied.get("updated", []))
        tot["created"] += len(applied.get("created", []))
        tot["held"] += len(keep_p)
        if keep_p:
            tri["proposals"], tri["new_tasks"] = keep_p, []
            store.update_inbox_triage(it["id"], tri)
        else:
            store.update_inbox_triage(it["id"], tri)
            store.set_inbox_status(it["id"], "confirmed")
    return tot


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


# ---- Deck rebrand (one-stop: drop a .pptx, get it back on-brand) ------------
_REBRAND_MAX = 80 * 1024 * 1024  # 80 MB — protects the small VM

@app.post("/api/rebrand")
async def rebrand_deck(file: UploadFile = File(...)):
    """Upload a .pptx -> download it re-skinned into the Taikun brand. Lossless
    (media/charts/embeds preserved); runs the in-process rebrand.rebrand_bytes."""
    name = file.filename or "deck.pptx"
    if not name.lower().endswith(".pptx"):
        raise HTTPException(400, "Please upload a PowerPoint .pptx file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file was empty.")
    if len(data) > _REBRAND_MAX:
        raise HTTPException(413, f"File too large (max {_REBRAND_MAX // (1024*1024)} MB).")
    try:
        out = await asyncio.to_thread(rebrand.rebrand_bytes, data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Rebrand failed: {e}")
    base = name[:-5] if name.lower().endswith(".pptx") else name
    dl = f"{base}-Taikun.pptx"
    return Response(content=out,
                    media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    headers={"Content-Disposition": f'attachment; filename="{dl}"'})


# ---- PDF OCR (one-stop: drop a scanned PDF, get it back searchable) ---------
_OCR_MAX = 40 * 1024 * 1024  # 40 MB — protects the small VM

@app.post("/api/ocr")
async def ocr_pdf(file: UploadFile = File(...)):
    """Upload a scanned/printed .pdf -> download a searchable PDF: the original
    pages are kept pixel-for-pixel and an AI-OCR'd invisible text layer is embedded
    over them. Renders pages -> gateway vision model -> embed, in ocr.ocr_pdf_bytes."""
    name = file.filename or "document.pdf"
    if not ocr.is_pdf(name, file.content_type):
        raise HTTPException(400, "Please upload a PDF file.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file was empty.")
    if len(data) > _OCR_MAX:
        raise HTTPException(413, f"File too large (max {_OCR_MAX // (1024*1024)} MB).")
    try:
        out, _text = await asyncio.to_thread(ocr.ocr_pdf_bytes, data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"OCR failed: {e}")
    base = name[:-4] if name.lower().endswith(".pdf") else name
    dl = f"{base}-searchable.pdf"
    return Response(content=out, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{dl}"'})


# Static board UI last, so /api/* and /health win. html=True serves index.html at /.
_static = Path(__file__).parent / "static"
if _static.exists():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PM_PORT", "8110")))
