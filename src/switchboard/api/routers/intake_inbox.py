"""Intake and inbox REST routes."""
from __future__ import annotations

import asyncio
import os
from typing import Callable

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile

import attachments
import inbox as inbox_mod
import intake
import store
import transcribe
from switchboard.domain.projects.context import ProjectContext


ProjectResolver = Callable[[str], str]


def _queue_triage(res, source, subject, project=None):
    """Persist a triage result into the Action Queue (Inbox) as a pending item so its proposed
    changes survive reload and are bulk-confirmable in one place — not just ephemeral chat cards.
    Only queues when there's something to act on. Mutates + returns `res` with inbox_id.
    The item lands on `project`'s inbox (same board the artifact was ingested on)."""
    if not (project or "").strip():
        raise ValueError("project required")
    try:
        if res and ((res.get("proposals")) or (res.get("new_tasks"))):
            triage = {"proposals": res.get("proposals", []), "new_tasks": res.get("new_tasks", []),
                      "sources": res.get("sources", []), "summary": res.get("summary", "")}
            res["inbox_id"] = store.add_inbox_item(
                source, source + "-" + os.urandom(6).hex(), "", subject or source,
                res.get("summary", ""), triage, project=project)
    except Exception:
        pass  # queueing is best-effort; the chat cards still work
    return res


def create_router(*, resolve_project: ProjectResolver, sibling_bc_only: bool = False) -> APIRouter:
    router = APIRouter()

    if not sibling_bc_only:
        @router.post("/api/intake")
        async def intake_artifact(body: dict = Body(...), project: str = Query(...)):
            """Ingest an artifact (transcript/email/document) into `project`'s RAG corpus + triage it
            against that board. Returns {summary, proposals, new_tasks, sources, ingested_chunks, inbox_id}."""
            text = (body.get("text") or "").strip()
            if not text:
                raise HTTPException(400, "text required")
            project = resolve_project(project)
            try:
                res = await asyncio.to_thread(
                    intake.ingest_and_triage, body.get("kind") or "note", body.get("title") or "", text,
                    project=project)
                return _queue_triage(res, body.get("kind") or "note", body.get("title") or "", project)
            except Exception as e:
                raise HTTPException(502, f"intake error: {e}")

    @router.post("/api/intake/upload")
    async def intake_upload(file: UploadFile = File(...), kind: str = Form("document"),
                            title: str = Form(""), project: str = Query(...)):
        """Drop a file — audio/video, pdf, docx, or text — extract or TRANSCRIBE it, then
        ingest into `project`'s corpus + triage. Media is transcribed via OpenAI (Whisper) through the
        gateway; everything else uses attachments.extract. Same response shape as /api/intake."""
        project = resolve_project(project)
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
            res = await asyncio.to_thread(intake.ingest_and_triage, kind or "document", label, text,
                                          project=project)
        except Exception as e:
            raise HTTPException(502, f"intake error: {e}")
        res["transcribed"] = media
        res["chars"] = len(text)
        return _queue_triage(res, "transcript" if media else "upload", label, project)

    if not sibling_bc_only:
        @router.get("/api/inbox")
        async def get_inbox(status: str = None, project: str = Query(...)):
            project = resolve_project(project)
            return {"items": store.list_inbox(status, project=project),
                    "pending": store.inbox_pending_count(project=project)}

    @router.post("/api/inbox/{item_id}/confirm")
    async def confirm_inbox(item_id: int, body: dict = Body(default={}),
                            project: str = Query(...)):
        """Apply the given proposals/new_tasks (default: all of the item's). `keep_proposals` /
        `keep_new_tasks` are held back and the item STAYS pending with just those (used to bulk-
        confirm the safe changes while holding status->Done items that still need evidence).
        Edited proposals are honored — the client sends the modified field values to apply."""
        project = resolve_project(project)
        item = store.get_inbox_item(item_id, project=project)
        if not item:
            raise HTTPException(404, "no such inbox item")
        tri = item.get("triage") or {}
        applied = inbox_mod.apply(body.get("proposals", tri.get("proposals", [])),
                                  body.get("new_tasks", tri.get("new_tasks", [])), project=project)
        keep_p = body.get("keep_proposals") or []
        keep_n = body.get("keep_new_tasks") or []
        tri["applied"] = applied
        if keep_p or keep_n:
            tri["proposals"], tri["new_tasks"] = keep_p, keep_n
            store.update_inbox_triage(item_id, tri, project=project)   # stays pending with the held items
        else:
            store.update_inbox_triage(item_id, tri, project=project)
            store.set_inbox_status(item_id, "confirmed", project=project)
        return {"applied": applied, "remaining": len(keep_p) + len(keep_n)}

    @router.post("/api/inbox/confirm_all")
    async def confirm_all_inbox(body: dict = Body(default={}), project: str = Query(...)):
        """Bulk-confirm pending queue items. safe_only=True applies everything EXCEPT status->Done
        proposals (which need acceptance evidence), holding those back so the item stays pending."""
        project = resolve_project(project)
        safe_only = bool(body.get("safe_only"))
        ids = body.get("ids")
        items = store.list_inbox("pending", limit=500, project=project)
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
            applied = inbox_mod.apply(apply_p, nts, project=project)
            tri["applied"] = applied
            tot["items"] += 1
            tot["updated"] += len(applied.get("updated", []))
            tot["created"] += len(applied.get("created", []))
            tot["held"] += len(keep_p)
            if keep_p:
                tri["proposals"], tri["new_tasks"] = keep_p, []
                store.update_inbox_triage(it["id"], tri, project=project)
            else:
                store.update_inbox_triage(it["id"], tri, project=project)
                store.set_inbox_status(it["id"], "confirmed", project=project)
        return tot

    @router.post("/api/inbox/{item_id}/dismiss")
    async def dismiss_inbox(item_id: int, project: str = Query(...)):
        project = resolve_project(project)
        if not store.get_inbox_item(item_id, project=project):
            raise HTTPException(404, "no such inbox item")
        store.set_inbox_status(item_id, "dismissed", project=project)
        return {"dismissed": item_id}

    @router.post("/api/inbox/simulate")
    async def simulate_inbox(body: dict = Body(...), project: str = Query(...)):
        """Inject a fake inbound email to exercise the Live Inbox pipeline without a mailbox. Routes
        to `project` (query param, or a `project` field in the body for explicit cross-board testing)."""
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        project = resolve_project(body.get("project") or project)
        # resolve_project is the ingress authority.  Thread its immutable result;
        # do not perform a second registry lookup below the router.
        project_context = ProjectContext(project_id=project, source="inbox-simulate")
        sender = body.get("sender") or "tester@taikunai.com"
        headers = {"from": sender, "to": body.get("to") or "", "cc": body.get("cc") or "",
                   "date": body.get("date") or "", "message_id": body.get("message_id") or ""}
        try:
            item = await asyncio.to_thread(
                inbox_mod.process, "email-sim", "sim-" + os.urandom(6).hex(),
                sender, body.get("subject") or "(simulated)", text, headers, None,
                project_context)
        except Exception as e:
            raise HTTPException(502, f"inbox error: {e}")
        return item or {"deduped": True}

    @router.post("/api/inbox/poll")
    async def poll_inbox_now(project: str = Query(...)):
        """Trigger mailbox poll. ``project`` is required for auth-scope (SEG-4);
        the shared poller routes messages via its own project index (SEG-2)."""
        resolve_project(project)
        import inbox_source
        return await asyncio.to_thread(inbox_source.poll)

    return router
