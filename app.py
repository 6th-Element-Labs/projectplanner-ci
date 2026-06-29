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
import hashlib
import hmac
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

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import agent  # noqa: E402
import attachments  # noqa: E402
import auth  # noqa: E402
import digest  # noqa: E402
import transcribe  # noqa: E402
import dispatch  # noqa: E402
import export  # noqa: E402
import inbox as inbox_mod  # noqa: E402
import intake  # noqa: E402
import github_sync  # noqa: E402
import notify  # noqa: E402
import ocr  # noqa: E402
import rebrand  # noqa: E402
import signals  # noqa: E402
import store  # noqa: E402

app = FastAPI(title="Taikun PM", version="0.1.0")

store.init_project_registry()
store.init_db()
_seeded = store.seed_if_empty()
# Additional projects — each in its OWN db file; one-shot seed, guarded so a restart never
# wipes or re-imports. Maxwell (DEFAULT_PROJECT) is seeded above, untouched.
for _pid in store.project_ids():
    if _pid != store.DEFAULT_PROJECT:
        try:
            store.init_db(_pid)
            store.seed_if_empty(_pid)
        except Exception as _e:  # never let a second project block startup
            print(f"[projects] seed {_pid} skipped: {_e}")


def _proj(project: str) -> str:
    """Validate a project id against the registry — fail closed (400) on anything unknown
    so a bad/stale id can never be silently routed to (or written into) the wrong db."""
    if not store.has_project(project):
        raise HTTPException(400, f"unknown project: {project}")
    return project


def _principal(request: Request, project: str, scopes=("write:ixp",), dev_actor: str = "web"):
    try:
        return auth.authenticate(_proj(project), auth.bearer_from_request(request), scopes, dev_actor=dev_actor)
    except PermissionError as e:
        status = 403 if "forbidden" in str(e) else 401
        raise HTTPException(status, str(e))


def _actor_from_request(request: Request, fallback: str = "user") -> str:
    p = getattr(request.state, "principal", None)
    return auth.actor(p) if p else fallback


@app.middleware("http")
async def _write_auth_boundary(request: Request, call_next):
    """Gate state-changing web/API writes when PM_AUTH_MODE=required.

    Protocol endpoints authenticate inside their handlers because their project lives in the
    JSON body. GitHub webhooks keep their HMAC check.
    """
    if request.method.upper() not in {"POST", "PATCH", "DELETE"}:
        return await call_next(request)
    path = request.url.path
    if path.startswith(("/ixp/", "/txp/", "/tally/")) or path == "/api/github/webhook":
        return await call_next(request)
    project = "switchboard" if path == "/api/projects" else (
        request.query_params.get("project") or store.DEFAULT_PROJECT)
    if not store.has_project(project):
        return JSONResponse({"detail": f"unknown project: {project}"}, status_code=400)
    required_scopes = ("write:system",) if path == "/api/projects" else ("write:tasks",)
    try:
        request.state.principal = auth.authenticate(
            project, auth.bearer_from_request(request), required_scopes, dev_actor="web")
    except PermissionError as e:
        status = 403 if "forbidden" in str(e) else 401
        return JSONResponse({"detail": str(e)}, status_code=status)
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "taikun-pm", "tasks": len(store.list_tasks()),
            "projects": store.project_ids()}


@app.get("/api/projects")
async def list_projects():
    """The project switcher's source of truth — [{id, label, pretitle}] + the default."""
    return {"projects": store.projects(), "default": store.DEFAULT_PROJECT}


@app.post("/api/projects")
async def create_project(request: Request, body: dict = Body(...)):
    principal = _principal(request, "switchboard", ("write:system",), dev_actor="web")
    created = store.create_project(
        name=body.get("name") or body.get("label") or "",
        project_id=body.get("project_id") or body.get("id") or "",
        label=body.get("label") or "",
        pretitle=body.get("pretitle") or "",
        github_repo=body.get("github_repo") or body.get("repo") or "",
        actor=auth.actor(principal),
    )
    if created.get("error"):
        raise HTTPException(400, created["error"])
    return created


@app.get("/api/board")
async def board(project: str = Query(store.DEFAULT_PROJECT)):
    return store.board_payload(_proj(project))


@app.get("/api/people")
async def people(project: str = Query(store.DEFAULT_PROJECT)):
    return {"people": store.get_meta("people", store.DEFAULT_PEOPLE, project=_proj(project))}


@app.get("/api/tasks")
async def list_tasks(workstream: str = None, status: str = None, assignee: str = None,
                     project: str = Query(store.DEFAULT_PROJECT)):
    return {"tasks": store.list_tasks(workstream, status, assignee, project=_proj(project))}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    t = store.get_task(task_id, project=_proj(project))
    if not t:
        raise HTTPException(404, "task not found")
    return t


@app.post("/api/tasks")
async def create_task(request: Request, body: dict = Body(...), project: str = Query(...)):
    actor = _actor_from_request(request, body.pop("_actor", "user"))
    t = store.create_task(body, actor=actor, project=_proj(project))
    if not t:
        raise HTTPException(400, "workstream_id and title are required")
    return t


@app.patch("/api/tasks/{task_id}")
async def patch_task(request: Request, task_id: str, body: dict = Body(...), project: str = Query(...)):
    actor = _actor_from_request(request, body.pop("_actor", "user"))
    t = store.update_task(task_id, body, actor=actor, project=_proj(project))
    if not t:
        raise HTTPException(404, "task not found")
    if t.get("error") == "done_requires_merge_provenance":
        raise HTTPException(409, t.get("message") or "Done requires merge provenance")
    return t


@app.post("/api/tasks/{task_id}/verify_offline")
async def verify_task_offline(request: Request, task_id: str, body: dict = Body(default={}),
                              project: str = Query(...)):
    actor = _actor_from_request(request, body.pop("_actor", "switchboard/operator"))
    result = store.mark_task_offline_done(
        task_id,
        evidence=body.get("evidence") or body.get("evidence_json") or {},
        artifact_url=body.get("artifact_url") or "",
        evidence_hash=body.get("evidence_hash") or body.get("hash") or "",
        verifier=body.get("verifier") or actor,
        reviewed_at=body.get("reviewed_at"),
        actor=actor,
        project=_proj(project),
    )
    if result.get("error") == "task not found":
        raise HTTPException(404, "task not found")
    if result.get("error"):
        raise HTTPException(409, result.get("message") or result["error"])
    return result


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, project: str = Query(...)):
    if not store.delete_task(task_id, project=_proj(project)):
        raise HTTPException(404, "task not found")
    return {"deleted": task_id}


@app.post("/api/tasks/{task_id}/archive")
async def archive_task(request: Request, task_id: str, body: dict = Body(default={}),
                       project: str = Query(...)):
    project = _proj(project)
    principal = _principal(request, "switchboard", ("write:system",), dev_actor="web")
    result = store.archive_task(
        task_id, reason=(body or {}).get("reason") or "",
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/tasks/{task_id}/move")
async def move_task(request: Request, task_id: str, body: dict = Body(...),
                    project: str = Query(...)):
    project_from = _proj(project)
    project_to = _proj((body or {}).get("project_to") or (body or {}).get("destination_project") or "")
    principal = _principal(request, "switchboard", ("write:system",), dev_actor="web")
    result = store.move_task(
        task_id, project_from=project_from, project_to=project_to,
        reason=(body or {}).get("reason") or "",
        actor=auth.actor(principal),
        new_task_id=(body or {}).get("new_task_id") or "",
        dependency_policy=(body or {}).get("dependency_policy") or "fail",
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/tasks/{task_id}/claims/{claim_id}/revoke")
async def api_revoke_claim(request: Request, task_id: str, claim_id: str,
                           body: dict = Body(default={}), project: str = Query(...)):
    project = _proj(project)
    body = body or {}
    actor = _actor_from_request(request, "switchboard/operator")
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
        actor=actor,
        project=project,
    )
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/api/tasks/{task_id}/comment")
async def comment(request: Request, task_id: str, body: dict = Body(...), project: str = Query(...)):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    t = store.add_comment(task_id, _actor_from_request(request, body.get("actor", "user")),
                          text, project=_proj(project))
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
async def chat(task_id: str, body: dict = Body(...), project: str = Query(store.DEFAULT_PROJECT)):
    """Per-task Ask Taikun agent: RAG over the plan docs + propose-then-confirm task edits."""
    project = _proj(project)
    assistant = {"helm": "Helm", "switchboard": "Switchboard"}.get(project, "Maxwell")
    task = store.get_task(task_id, project=project)
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
    store.add_comment(task_id, "user", msg, kind="chat", project=project)
    try:
        result = await asyncio.to_thread(agent.run, task, msg, history, project=project)
    except Exception as e:
        store.add_comment(task_id, assistant, f"(agent error: {e})", kind="chat", project=project)
        raise HTTPException(502, f"agent error: {e}")
    answer = result.get("answer") or ""
    store.add_comment(task_id, assistant, answer, kind="chat", project=project)
    return {"answer": answer, "proposal": result.get("proposal"), "sources": result.get("sources", [])}


@app.post("/api/chat")
async def plan_chat(body: dict = Body(...), project: str = Query(store.DEFAULT_PROJECT)):
    """Plan-wide Ask Taikun: the global agent sees the whole board + docs; propose-to-confirm."""
    project = _proj(project)
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message required")
    session = body.get("session") or "plan"
    history = [{"role": m["role"], "content": m["content"]}
               for m in store.recent_chat(session, 16, project=project) if m.get("content")]
    store.add_chat(session, "user", msg, project=project)
    try:
        result = await asyncio.to_thread(agent.run, None, msg, history, project=project)
    except Exception as e:
        store.add_chat(session, "assistant", f"(agent error: {e})", project=project)
        raise HTTPException(502, f"agent error: {e}")
    answer = result.get("answer") or ""
    store.add_chat(session, "assistant", answer,
                   {"proposals": result.get("proposals", []), "sources": result.get("sources", [])},
                   project=project)
    return {"answer": answer, "proposal": result.get("proposal"),
            "proposals": result.get("proposals", []), "sources": result.get("sources", [])}


@app.get("/api/chat/history")
async def plan_chat_history(session: str = "plan", project: str = Query(store.DEFAULT_PROJECT)):
    return {"messages": store.recent_chat(session, 100, project=_proj(project))}


@app.delete("/api/chat")
async def clear_plan_chat(session: str = "plan", project: str = Query(store.DEFAULT_PROJECT)):
    store.clear_chat(session, project=_proj(project))
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
async def plan_signals(project: str = Query(store.DEFAULT_PROJECT)):
    """Derived plan health: overdue / due-soon / blocked / ready / critical-slip /
    past-due decisions + each owner's next-best 1-2 tasks."""
    return signals.compute_plan_signals(project=_proj(project))


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


def _filtered_payload(workstream=None, owner=None, risk=None, blocking=0, q=None, person=None,
                      project="maxwell"):
    """Same filter semantics as the board UI, so 'export = what you see'."""
    p = store.board_payload(_proj(project))
    ql = (q or "").lower()
    people = store.get_meta("people", store.DEFAULT_PEOPLE, project=project) if person else []

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
async def export_xlsx(workstream: str = None, owner: str = None, risk: str = None, blocking: int = 0, q: str = None, person: str = None, project: str = Query(store.DEFAULT_PROJECT)):
    data = export.export_xlsx(_filtered_payload(workstream, owner, risk, blocking, q, person, _proj(project)))
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="project-plan.xlsx"'})


@app.get("/api/export.xml")
async def export_xml(workstream: str = None, owner: str = None, risk: str = None, blocking: int = 0, q: str = None, person: str = None, project: str = Query(store.DEFAULT_PROJECT)):
    xml = export.export_mspdi(_filtered_payload(workstream, owner, risk, blocking, q, person, _proj(project)))
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


# ---- Switchboard runtime protocol (IXP core + first TXP/OXP slices) ---------

def _body_project(body: dict) -> str:
    return _proj((body or {}).get("project") or store.DEFAULT_PROJECT)


@app.post("/ixp/v1/register_agent")
async def ixp_register_agent(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    agent_id = (body.get("agent_id") or "").strip()
    runtime = (body.get("runtime") or "").strip()
    if not agent_id or not runtime:
        raise HTTPException(400, "agent_id and runtime required")
    return store.register_agent(
        agent_id=agent_id, runtime=runtime, model=body.get("model") or "",
        lane=body.get("lane") or "", task_id=body.get("task") or body.get("task_id") or "",
        ttl_s=int(body.get("ttl_s") or 120), control=body.get("control") or {},
        protocol=body.get("protocol") or {},
        principal_id=principal["id"], actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/heartbeat")
async def ixp_heartbeat(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    return store.heartbeat((body.get("agent_id") or "").strip(),
                           actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/agents")
async def ixp_agents(project: str = Query(store.DEFAULT_PROJECT), lane: str = ""):
    return {"agents": store.list_active_agents(lane=lane, project=_proj(project))}


@app.post("/ixp/v1/register_host")
async def ixp_register_host(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    return store.register_host(body, principal_id=principal["id"],
                               actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/heartbeat_host")
async def ixp_heartbeat_host(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    return store.heartbeat_host(
        (body.get("host_id") or "").strip(),
        active_sessions=body.get("active_sessions"),
        capacity=body.get("capacity") or {},
        status=body.get("status") or "online",
        last_error=body.get("last_error") or "",
        actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/agent_hosts")
async def ixp_agent_hosts(project: str = Query(store.DEFAULT_PROJECT), runtime: str = "",
                          lane: str = "", capability: str = "",
                          include_stale: bool = False):
    return {"hosts": store.list_agent_hosts(runtime=runtime, lane=lane,
                                            capability=capability,
                                            include_stale=include_stale,
                                            project=_proj(project))}


@app.get("/ixp/v1/host_status")
async def ixp_host_status(host_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    status = store.host_status(host_id, project=_proj(project))
    if status.get("error"):
        raise HTTPException(404, status["error"])
    return status


@app.get("/ixp/v1/runner_sessions")
async def ixp_runner_sessions(project: str = Query(store.DEFAULT_PROJECT),
                              host_id: str = "", runtime: str = "",
                              task_id: str = "", status: str = "",
                              include_stale: bool = False):
    return {"sessions": store.list_runner_sessions(
        host_id=host_id, runtime=runtime, task_id=task_id, status=status,
        include_stale=include_stale, project=_proj(project))}


@app.post("/ixp/v1/register_runner_session")
async def ixp_register_runner_session(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or body.get("agent_id") or "runner")
    record = dict(body)
    record.pop("project", None)
    return store.upsert_runner_session(
        record, principal_id=principal["id"], actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/heartbeat_runner_session")
async def ixp_heartbeat_runner_session(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or body.get("agent_id") or "runner")
    record = dict(body)
    record.pop("project", None)
    return store.upsert_runner_session(
        record, principal_id=principal["id"], actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/request_runner_snapshot")
async def ixp_request_runner_snapshot(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "snapshot",
        reason=body.get("reason") or "",
        options=body.get("options") or {},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/request_runner_kill")
async def ixp_request_runner_kill(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "kill",
        reason=body.get("reason") or "",
        options={"grace_seconds": body.get("grace_seconds"),
                 "signal": body.get("signal") or "TERM"},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/request_runner_restart")
async def ixp_request_runner_restart(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/operator")
    result = store.request_runner_control(
        body.get("runner_session_id") or body.get("id") or "",
        "restart",
        reason=body.get("reason") or "",
        options=body.get("options") or {},
        actor=auth.actor(principal), principal_id=principal["id"], project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/ixp/v1/runner_controls")
async def ixp_runner_controls(project: str = Query(store.DEFAULT_PROJECT),
                              status: str = "", host_id: str = "",
                              runner_session_id: str = ""):
    return {"requests": store.list_runner_control_requests(
        status=status, host_id=host_id, runner_session_id=runner_session_id,
        project=_proj(project))}


@app.post("/ixp/v1/claim_runner_control")
async def ixp_claim_runner_control(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    result = store.claim_runner_control_request(
        (body.get("host_id") or "").strip(),
        (body.get("request_id") or body.get("id") or "").strip(),
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/complete_runner_control")
async def ixp_complete_runner_control(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    result = store.complete_runner_control_request(
        (body.get("request_id") or body.get("id") or "").strip(),
        result=body.get("result") or {},
        snapshot=body.get("snapshot") or {},
        status=body.get("status") or "",
        actor=auth.actor(principal), project=project)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.post("/ixp/v1/claim")
async def ixp_claim(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("agent_id") or "agent")
    names = body.get("names") or body.get("files") or []
    if isinstance(names, str):
        names = [x.strip() for x in names.replace("\n", ",").split(",") if x.strip()]
    return store.claim_resources(
        agent_id=(body.get("agent_id") or auth.actor(principal)).strip(),
        resource_type=(body.get("resource_type") or "file").strip(),
        names=names, task_id=body.get("task") or body.get("task_id"),
        ttl_seconds=int(body.get("ttl_s") or body.get("ttl_seconds") or
                        (int(body.get("ttl_min") or 30) * 60)),
        principal_id=principal["id"], actor=auth.actor(principal),
        idem_key=body.get("idem_key") or "", project=project)


@app.post("/ixp/v1/check")
async def ixp_check(body: dict = Body(...)):
    project = _body_project(body)
    names = body.get("names") or body.get("files") or []
    if isinstance(names, str):
        names = [x.strip() for x in names.replace("\n", ",").split(",") if x.strip()]
    return {"held": store.check_resources((body.get("resource_type") or "file").strip(),
                                           names, project=project)}


@app.post("/ixp/v1/release")
async def ixp_release(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.release_resource_lease((body.get("lease_id") or "").strip(),
                                        actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/leases")
async def ixp_leases(project: str = Query(store.DEFAULT_PROJECT)):
    return {"leases": store.list_active_resource_leases(project=_proj(project))}


@app.post("/ixp/v1/send")
async def ixp_send(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("from_agent") or "agent")
    return store.send_agent_message(
        from_agent=body.get("from_agent") or auth.actor(principal),
        to_agent=body.get("to_agent") or body.get("to") or "",
        message=body.get("message") or "",
        task_id=body.get("task") or body.get("task_id"),
        requires_ack=bool(body.get("requires_ack")),
        ack_deadline_minutes=body.get("ack_deadline_minutes"),
        ack_timeout_seconds=(body.get("ack_timeout_seconds")
                             if body.get("ack_timeout_seconds") is not None
                             else body.get("ack_timeout_s")),
        on_ack_timeout=(body.get("on_ack_timeout") or body.get("ack_timeout_action") or
                        "notify_sender"),
        signal=body.get("signal"), priority=int(body.get("priority") or 0),
        principal_id=principal["id"], idem_key=body.get("idem_key") or "",
        project=project)


@app.get("/ixp/v1/inbox")
async def ixp_inbox(project: str = Query(store.DEFAULT_PROJECT),
                    to_agent: str = "", unacked: bool = True, signal: str = ""):
    msgs = store.list_unacked_messages(to_agent, project=_proj(project)) if unacked else []
    if signal:
        msgs = [m for m in msgs if m.get("signal") == signal]
    return {"messages": msgs}


@app.post("/ixp/v1/ack")
async def ixp_ack(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.ack_message(int(body.get("message_id") or body.get("id")),
                             response=body.get("response") or "",
                             actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/message_status")
async def ixp_message_status(message_id: int, project: str = Query(store.DEFAULT_PROJECT)):
    msg = store.get_message_status(message_id, project=_proj(project))
    if not msg:
        raise HTTPException(404, "message not found")
    return msg


@app.get("/ixp/v1/pending_acks")
async def ixp_pending_acks(project: str = Query(store.DEFAULT_PROJECT), agent_id: str = ""):
    return {"pending_acks": store.list_pending_acks(agent_id=agent_id, project=_proj(project))}


@app.get("/ixp/v1/monitors")
async def ixp_monitors(project: str = Query(store.DEFAULT_PROJECT), status: str = "",
                       kind: str = ""):
    return {"monitors": store.list_coordination_monitors(status=status, kind=kind,
                                                         project=_proj(project))}


@app.post("/ixp/v1/sweep_monitors")
async def ixp_sweep_monitors(request: Request, body: dict = Body(default={})):
    project = _body_project(body or {})
    _principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
    return store.sweep_coordination_monitors(project=project)


@app.post("/ixp/v1/reconcile_alerts")
async def ixp_reconcile_alerts(request: Request, body: dict = Body(default={})):
    project = _body_project(body or {})
    _principal(request, project, ("write:ixp",), dev_actor="switchboard/reconcile")
    return store.run_reconcile_alerts(
        project=project,
        alert_to=body.get("alert_to") or "switchboard/operator",
        min_severity=body.get("min_severity") or "medium")


@app.post("/ixp/v1/resolve_monitor")
async def ixp_resolve_monitor(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
    return store.resolve_monitor(body.get("monitor_id") or body.get("id") or "",
                                 reason=body.get("reason") or "manual",
                                 actor=auth.actor(principal), project=project)


@app.post("/ixp/v1/cancel_monitor")
async def ixp_cancel_monitor(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="switchboard/monitor")
    return store.cancel_monitor(body.get("monitor_id") or body.get("id") or "",
                                reason=body.get("reason") or "cancelled",
                                actor=auth.actor(principal), project=project)


@app.get("/ixp/v1/delta")
async def ixp_delta(project: str = Query(store.DEFAULT_PROJECT), lane: str = "",
                    since_cursor: int = 0):
    return store.get_activity_delta(since_cursor=since_cursor, lane=lane, project=_proj(project))


@app.get("/ixp/v1/working_agreement")
async def ixp_working_agreement(project: str = Query(store.DEFAULT_PROJECT)):
    return store.get_working_agreement(project=_proj(project))


@app.post("/txp/v1/claim_next")
async def txp_claim_next(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor=body.get("agent_id") or "agent")
    lanes = store.coerce_csv_list(body.get("lanes"))
    if not lanes:
        lanes = store.coerce_csv_list(body.get("lane"))
    return store.claim_next(
        agent_id=body.get("agent_id") or auth.actor(principal),
        lanes=lanes,
        capabilities=store.coerce_csv_list(body.get("capabilities")),
        max_risk=body.get("max_risk") or "",
        max_budget_usd=body.get("max_budget_usd"),
        principal_id=principal["id"], actor=auth.actor(principal),
        ttl_seconds=int(body.get("ttl_s") or body.get("ttl_seconds") or 1800),
        idem_key=body.get("idem_key") or "", project=project)


@app.post("/txp/v1/claim_task")
async def txp_claim_task(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor=body.get("agent_id") or "agent")
    return store.claim_task(
        task_id=body.get("task_id") or body.get("task") or "",
        agent_id=body.get("agent_id") or auth.actor(principal),
        principal_id=principal["id"], actor=auth.actor(principal),
        ttl_seconds=int(body.get("ttl_s") or body.get("ttl_seconds") or 1800),
        idem_key=body.get("idem_key") or "", project=project)


@app.post("/txp/v1/request_wake")
async def txp_request_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("source") or "agent")
    return store.request_wake(
        selector=body.get("selector") or {},
        reason=body.get("reason") or "",
        source=body.get("source") or auth.actor(principal),
        policy=body.get("policy") or {},
        task_id=body.get("task") or body.get("task_id"),
        principal_id=principal["id"], actor=auth.actor(principal),
        idem_key=body.get("idem_key") or "", project=project)


@app.get("/txp/v1/list_wake_intents")
async def txp_list_wake_intents(project: str = Query(store.DEFAULT_PROJECT),
                                status: str = "", host_id: str = "",
                                runtime: str = ""):
    return {"wake_intents": store.list_wake_intents(status=status, host_id=host_id,
                                                    runtime=runtime, project=_proj(project))}


@app.post("/txp/v1/claim_wake")
async def txp_claim_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or "agent-host")
    return store.claim_wake((body.get("host_id") or "").strip(),
                            (body.get("wake_id") or body.get("id") or "").strip(),
                            actor=auth.actor(principal), project=project)


@app.post("/txp/v1/complete_wake")
async def txp_complete_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("host_id") or body.get("agent_id") or "agent-host")
    return store.complete_wake(
        (body.get("wake_id") or body.get("id") or "").strip(),
        runner_session_id=body.get("runner_session_id") or "",
        agent_id=body.get("agent_id") or "",
        result=body.get("result") or {},
        actor=auth.actor(principal), project=project)


@app.post("/txp/v1/cancel_wake")
async def txp_cancel_wake(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.cancel_wake((body.get("wake_id") or body.get("id") or "").strip(),
                             reason=body.get("reason") or "cancelled",
                             actor=auth.actor(principal), project=project)


@app.post("/txp/v1/complete_claim")
async def txp_complete_claim(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.complete_claim(body.get("claim_id") or "", evidence=body.get("evidence") or {},
                                final_status=body.get("final_status") or "",
                                actor=auth.actor(principal), project=project)


@app.post("/txp/v1/abandon_claim")
async def txp_abandon_claim(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="agent")
    return store.abandon_claim(body.get("claim_id") or "", reason=body.get("reason") or "unspecified",
                               actor=auth.actor(principal), project=project)


@app.post("/txp/v1/revoke_claim")
async def txp_revoke_claim(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("operator_agent") or "switchboard/operator")
    sort_order = body.get("sort_order")
    try:
        sort_order_value = int(sort_order) if sort_order not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "sort_order must be an integer")
    return store.revoke_claim(
        body.get("claim_id") or "",
        reason=body.get("reason") or "operator override",
        reassign_to=body.get("reassign_to") or body.get("reassigned_to") or "",
        sort_order=sort_order_value,
        partial_evidence=body.get("partial_evidence") or body.get("evidence") or {},
        notify=body.get("notify") is not False,
        ack_deadline_minutes=float(body.get("ack_deadline_minutes") or 5),
        actor=auth.actor(principal),
        project=project,
    )


@app.post("/tally/v1/spend/ingest")
async def tally_spend_ingest(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor=body.get("agent_id") or "tally")
    return store.report_usage(
        source=body.get("source") or "agent_report",
        confidence=body.get("confidence") or "reported",
        task_id=body.get("task_id"), claim_id=body.get("claim_id"),
        outcome_id=body.get("outcome_id"), agent_id=body.get("agent_id"),
        principal_id=principal["id"], runtime=body.get("runtime") or "",
        call_site=body.get("call_site") or "", provider=body.get("provider") or "",
        model=body.get("model") or "", prompt_tokens=int(body.get("prompt_tokens") or 0),
        completion_tokens=int(body.get("completion_tokens") or 0),
        total_tokens=body.get("total_tokens"), cost_usd=float(body.get("cost_usd") or 0.0),
        latency_ms=body.get("latency_ms"), status=body.get("status") or "ok",
        metadata=body.get("metadata") or {}, request_id=body.get("request_id"),
        project=project)


@app.post("/tally/v1/outcomes")
async def tally_record_outcome(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",),
                           dev_actor=body.get("actor") or body.get("agent_id") or "tally")
    return store.record_outcome(
        outcome_type=body.get("type") or body.get("outcome_type") or "",
        title=body.get("title") or "",
        task_id=body.get("task_id") or body.get("task"),
        claim_id=body.get("claim_id"),
        epic_id=body.get("epic_id") or body.get("epic"),
        status=body.get("status") or "proposed",
        verifier=body.get("verifier") or "",
        verification=body.get("verification") or "",
        evidence=body.get("evidence") or {},
        value=body.get("value") or {},
        actor=auth.actor(principal),
        project=project)


@app.post("/tally/v1/outcomes/{outcome_id}/verify")
async def tally_verify_outcome(outcome_id: str, request: Request, body: dict = Body(default={})):
    project = _body_project(body or {})
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    return store.verify_outcome(
        outcome_id,
        verifier=body.get("verifier") or auth.actor(principal),
        verification=body.get("verification") or "",
        evidence=body.get("evidence") or {},
        actor=auth.actor(principal),
        project=project)


@app.post("/tally/v1/outcomes/{outcome_id}/reject")
async def tally_reject_outcome(outcome_id: str, request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    return store.reject_outcome(
        outcome_id,
        verifier=body.get("verifier") or auth.actor(principal),
        reason=body.get("reason") or "rejected",
        actor=auth.actor(principal),
        project=project)


@app.post("/tally/v1/kpis")
async def tally_create_kpi(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    return store.create_kpi(
        name=body.get("name") or "",
        unit=body.get("unit") or "",
        direction=body.get("direction") or "",
        owner=body.get("owner") or "",
        baseline_value=body.get("baseline_value"),
        current_value=body.get("current_value"),
        target_value=body.get("target_value"),
        period=body.get("period") or "",
        actor=auth.actor(principal),
        project=project)


@app.patch("/tally/v1/kpis/{kpi_id}")
async def tally_update_kpi(kpi_id: str, request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    if body.get("current_value") is None:
        raise HTTPException(400, "current_value is required")
    return store.update_kpi_value(
        kpi_id,
        current_value=float(body.get("current_value")),
        evidence=body.get("evidence") or {},
        actor=auth.actor(principal),
        project=project)


@app.post("/tally/v1/outcome_kpi_links")
async def tally_link_outcome_kpi(request: Request, body: dict = Body(...)):
    project = _body_project(body)
    principal = _principal(request, project, ("write:ixp",), dev_actor="tally")
    return store.link_outcome_to_kpi(
        outcome_id=body.get("outcome_id") or "",
        kpi_id=body.get("kpi_id") or "",
        contribution=body.get("contribution"),
        contribution_unit=body.get("contribution_unit") or "",
        confidence=body.get("confidence") or "directional",
        rationale=body.get("rationale") or "",
        actor=auth.actor(principal),
        project=project)


@app.get("/tally/v1/task/{task_id}")
async def tally_task(task_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    return store.task_tally(task_id, project=_proj(project))


@app.get("/tally/v1/kpi/{kpi_id}")
async def tally_kpi(kpi_id: str, project: str = Query(store.DEFAULT_PROJECT)):
    return store.kpi_tally(kpi_id, project=_proj(project))


@app.get("/tally/v1/project")
async def tally_project(project: str = Query(store.DEFAULT_PROJECT)):
    return store.project_tally(project=_proj(project))


@app.get("/ixp/v1/reconcile")
async def ixp_reconcile(project: str = Query(store.DEFAULT_PROJECT)):
    return store.reconcile(project=_proj(project))


# ---- GitHub webhook — §1.2 board↔git auto-sync + §1.3 "main moved" notify ----
# Configure in GitHub → repo Settings → Webhooks:
#   Payload URL: https://<your-host>/api/github/webhook
#   Content type: application/json
#   Secret: match PM_GITHUB_WEBHOOK_SECRET in .env
#   Events: push + pull_request (merged)
#
# Behaviour:
#   push to main/master   → find active leases on changed files, send directed IM
#                           to each lease holder. Does NOT mark tasks Done.
#   PR opened/synced      → record PR provenance + move branch/title/closing-referenced tasks
#                           to In Review; update head SHA after branch pushes. Broad body
#                           mentions are ignored.
#   PR merged             → stamp merged_sha + mark branch/title/closing-referenced tasks Done.

_GH_SECRET = os.environ.get("PM_GITHUB_WEBHOOK_SECRET", "")


def _verify_gh_signature(body: bytes, sig_header: str) -> bool:
    """HMAC-SHA256 signature check — skip if no secret configured (dev mode)."""
    if not _GH_SECRET:
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(_GH_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", sig_header)


async def _handle_push(payload: dict, project: str):
    return await asyncio.to_thread(github_sync.handle_push, payload, project)


async def _handle_pr(payload: dict, project: str):
    return await asyncio.to_thread(github_sync.handle_pr, payload, project)


@app.post("/api/github/webhook")
async def github_webhook(request: Request, project: str = ""):
    """Receive GitHub push/pull_request events. project selects the board to update.
    Set PM_GITHUB_WEBHOOK_SECRET in .env and configure the matching secret in GitHub."""
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_gh_signature(body, sig):
        raise HTTPException(401, "invalid webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON payload")

    project = github_sync.resolve_project(payload, project)
    _proj(project)  # fail-closed on unknown project
    if event == "push":
        result = await _handle_push(payload, project)
    elif event == "pull_request":
        result = await _handle_pr(payload, project)
    else:
        result = {"action": "ignored", "event": event}

    return JSONResponse(result)


# Static board UI last, so /api/* and /health win. html=True serves index.html at /.
_static = Path(__file__).parent / "static"
if _static.exists():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PM_PORT", "8110")))
