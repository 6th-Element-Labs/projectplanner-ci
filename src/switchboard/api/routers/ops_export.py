"""Export, audit, cleanup, rebrand, and OCR REST routes."""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

import auth
import export
import ocr
import rebrand
import store


ProjectResolver = Callable[[str], str]
PrincipalResolver = Callable[..., dict]
BodyProjectResolver = Callable[[dict], str]

_REBRAND_MAX = 80 * 1024 * 1024  # 80 MB — protects the small VM
_OCR_MAX = 40 * 1024 * 1024  # 40 MB — protects the small VM


def _people_of(t, people):
    """Owner-person(s) for a task — match the people list against owner_person_or_role.
    Mirrors the board UI's _peopleOf so 'export = what you see' for the owner filter."""
    owner = (t.get("owner_person_or_role") or "").lower()
    if not owner:
        return ["Unassigned"]
    m = [p for p in people if p.lower() in owner]
    return m or ["Unassigned"]


def _filtered_payload(workstream=None, owner=None, risk=None, blocking=0, q=None, person=None,
                      project="maxwell", *, _resolve_project: ProjectResolver):
    """Same filter semantics as the board UI, so 'export = what you see'."""
    p = store.board_payload(_resolve_project(project))
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


def create_router(*, resolve_project: ProjectResolver,
                  resolve_principal: PrincipalResolver,
                  resolve_body_project: BodyProjectResolver) -> APIRouter:
    router = APIRouter()

    @router.get("/api/export.xlsx")
    async def export_xlsx(workstream: str = None, owner: str = None, risk: str = None, blocking: int = 0, q: str = None, person: str = None, project: str = Query(store.DEFAULT_PROJECT)):
        data = export.export_xlsx(_filtered_payload(workstream, owner, risk, blocking, q, person, resolve_project(project), _resolve_project=resolve_project))
        return Response(content=data,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="project-plan.xlsx"'})

    @router.get("/api/export.xml")
    async def export_xml(workstream: str = None, owner: str = None, risk: str = None, blocking: int = 0, q: str = None, person: str = None, project: str = Query(store.DEFAULT_PROJECT)):
        xml = export.export_mspdi(_filtered_payload(workstream, owner, risk, blocking, q, person, resolve_project(project), _resolve_project=resolve_project))
        return Response(content=xml, media_type="text/xml",
                        headers={"Content-Disposition": 'attachment; filename="project-plan.xml"'})

    @router.get("/api/audit/export")
    async def audit_export(request: Request, project: str = Query(store.DEFAULT_PROJECT)):
        project = resolve_project(project)
        resolve_principal(request, project, ("write:system",), dev_actor="auditor")
        data = store.audit_export(project=project)
        return JSONResponse(
            data,
            headers={"Content-Disposition": f'attachment; filename="{project}-audit-export.json"'},
        )

    @router.get("/api/cleanup/candidates")
    async def cleanup_candidates(request: Request, project: str = Query(store.DEFAULT_PROJECT),
                                 kinds: str = "", proof_task_age_days: float = 14):
        project = resolve_project(project)
        resolve_principal(request, project, ("write:system",), dev_actor="switchboard/operator")
        data = store.cleanup_candidates(
            project=project,
            proof_task_age_days=proof_task_age_days,
            include_kinds=store.coerce_csv_list(kinds),
        )
        if data.get("error"):
            raise HTTPException(400, data)
        return data

    @router.post("/api/cleanup/apply")
    async def apply_cleanup(request: Request, body: dict = Body(default={})):
        body = body or {}
        project = resolve_body_project(body)
        principal = resolve_principal(request, project, ("write:system",), dev_actor="switchboard/operator")
        result = store.apply_cleanup(
            project=project,
            candidate_ids=store.coerce_csv_list(body.get("candidate_ids") or body.get("ids") or []),
            dry_run=body.get("dry_run") is not False,
            actor=auth.actor(principal),
            reason=body.get("reason") or "operator lifecycle cleanup",
            proof_task_age_days=float(body.get("proof_task_age_days") or 14),
            include_kinds=store.coerce_csv_list(body.get("kinds") or body.get("include_kinds") or []),
        )
        if result.get("error"):
            raise HTTPException(400, result)
        return result

    @router.post("/api/rebrand")
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

    @router.post("/api/ocr")
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

    return router
