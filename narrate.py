#!/usr/bin/env python3
"""CEO-voice task narrator — NARRATE-2 (see docs/CEO-NARRATOR-CONTRACT.md).

A SECOND narrator, separate from summarize.py and from the task-scoping agents. Agents write
tasks; this job reads them afterward and produces 3-4 sentences of plain-English, CEO-facing
prose for the task-detail tab. Stored in task_narrations (NOT task_summaries) — different
audience, different voice, different store.

Cost discipline (keeps the OpenAI bill negligible):
  * cheap model by default (PM_NARRATE_MODEL -> taikun-summarize = gpt-4o-mini);
  * driven by the pending_narrations trigger queue, so only tasks that had a MEANINGFUL status
    transition are considered — not every task with new activity;
  * a source fingerprint + activity cursor mean an idle re-run makes ZERO API calls.

Run via: python jobs.py narrate_pending
Or directly: python narrate.py [task_id [project]]   (one-shot / debugging)
"""
import json
import os
import sys
import time
from typing import List, Optional

import httpx

import store

BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
NARRATE_MODEL = os.environ.get("PM_NARRATE_MODEL", "taikun-summarize")  # cheap gpt-4o-mini
MIN_INTERVAL = int(os.environ.get("PM_NARRATE_INTERVAL", "45"))  # seconds between re-runs per task
MAX_TOKENS = int(os.environ.get("PM_NARRATE_MAX_TOKENS", "220"))  # ~3-4 sentences
MAX_TASKS = int(os.environ.get("PM_NARRATE_MAX_TASKS", "40"))     # per-run ceiling


def _trigger_statuses() -> Optional[set]:
    """Which transitions earn a narration. Empty/`*`/`all` = narrate every status change.
    Default set matches the contract; 'create' always qualifies via the enqueue reason."""
    raw = os.environ.get("PM_NARRATE_TRIGGERS", "create,In Review,Done,Blocked").strip()
    if not raw or raw.lower() in ("*", "all"):
        return None
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


_SYSTEM = (
    "Write a short user story for this one task, using ONLY the facts given. "
    "Format exactly:\n"
    "As a <who>, I want <what>, so that <why>.\n\n"
    "Then 2-3 plain sentences: current status, what is done or left, and the outcome when shipped. "
    "No jargon, no headers, no bullet points, no task IDs. Output ONLY the user story and follow-on."
)

_DELIVERABLE_SYSTEM = (
    "Write a short user story for this one deliverable, using ONLY the facts given. "
    "Format exactly:\n"
    "As a <who>, I want <what>, so that <why>.\n\n"
    "Then 2-3 plain sentences covering progress, what is done, what remains, and what shipping "
    "unlocks. Prefer As a from the operator/CEO audience; I want from the end state; so that from "
    "why it matters. Do not invent progress. No jargon, no headers, no bullets. "
    "Output ONLY the user story and follow-on."
)


def _gateway() -> tuple[str, str]:
    """Read gateway URL/key at call time (not import time) so systemd/.env stays authoritative."""
    base = (os.environ.get("PM_LLM_BASE_URL") or BASE or "http://127.0.0.1:8095/v1").rstrip("/")
    key = (os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY") or KEY or "").strip()
    return base, key


def _llm(context: str, system: str = _SYSTEM, meta: Optional[dict] = None) -> str:
    base, key = _gateway()
    if not key:
        raise RuntimeError("missing_llm_gateway_key")
    body = {"model": NARRATE_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": context}],
            "max_tokens": MAX_TOKENS}
    if meta:
        # UI-12: attribution rides on LiteLLM metadata so the gateway callback can
        # roll this call's provider-actual spend onto the right task/deliverable.
        body["metadata"] = meta
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=body,
        timeout=30,
    )
    if r.status_code >= 400:
        detail = (r.text or "").strip().replace("\n", " ")[:180]
        raise RuntimeError(f"llm_http_{r.status_code}:{detail or r.reason_phrase}")
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("llm_empty_choices")
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("llm_empty_content")
    return text


def _task_context(t: dict) -> str:
    """Build the narrator's task prompt from plan data already on the board — no code/PR
    fetch. NARRATE-6 widened this from title+status+desc to also include the deliverable /
    exit criteria (what 'done' means), dependency *titles* (not just ids), and the merged
    PR/commit subject (what was actually shipped), so 'what it is / what was delivered' has
    real material instead of a bare title."""
    prov = t.get("provenance") or {}
    git = t.get("git_state") or {}
    ev = git.get("evidence") or {}
    deps = (t.get("dependency_state") or {}).get("dependencies") or []
    dep_line = "; ".join(
        f"{d.get('task_id')} — {d.get('title', '')} [{d.get('status', '')}]" for d in deps
    ) or (", ".join(t.get("depends_on") or []) or "nothing")
    pr_url = prov.get("pr_url") or git.get("pr_url") or ""
    pr_subject = (ev.get("subject") or "").strip()
    activity = t.get("activity") or []
    parts = [
        f"Task: {t.get('title', '')}",
        f"Workstream: {t.get('_wsId') or '—'} · owner {t.get('owner_person_or_role') or t.get('owner_org') or '—'}",
        f"Status: {t.get('status', '')} (phase {t.get('phase') or '—'}, risk {t.get('risk_level') or '—'})",
        f"Provenance: {prov.get('label') or prov.get('type') or 'none'}" + (f" — {pr_url}" if pr_url else ""),
    ]
    if pr_subject:
        parts.append(f"Merged PR/commit summary: {pr_subject[:400]}")
    parts.append(f"Description: {(t.get('description') or '')[:900] or '—'}")
    if t.get("deliverable"):
        parts.append(f"Deliverable (definition of done): {str(t.get('deliverable'))[:400]}")
    if t.get("exit_criteria"):
        parts.append(f"Exit criteria: {str(t.get('exit_criteria'))[:400]}")
    if t.get("entry_criteria"):
        parts.append(f"Entry criteria: {str(t.get('entry_criteria'))[:300]}")
    parts.append(f"Depends on: {dep_line}")
    parts.append(f"Recent activity (last 30 of {len(activity)}):\n{_activity_text(activity)}")
    return "\n".join(parts)


def _activity_text(activity: list) -> str:
    lines = []
    for a in activity[-30:]:
        kind = a.get("kind", "")
        actor = a.get("actor", "")
        payload = a.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        text = payload.get("text") or ""
        fields = {k: v for k, v in payload.items() if k != "text" and v is not None}
        if text:
            lines.append(f"[{kind}/{actor}] {text[:200]}")
        elif fields:
            changed = ", ".join(f"{k}={v}" for k, v in list(fields.items())[:4])
            lines.append(f"[{kind}/{actor}] {changed}")
    return "\n".join(lines) if lines else ""


def narrate_task(task_id: str, project: str = store.DEFAULT_PROJECT,
                 force: bool = False, _llm_fn=None) -> Optional[dict]:
    """Narrate one task. Returns the narration dict, or None if skipped (no such task, or
    nothing changed since the last narration). _llm_fn is injectable for tests."""
    t = store.get_task(task_id, project=project)
    if not t:
        return None

    activity = t.get("activity") or []
    last_cursor = max((a.get("id", 0) for a in activity), default=0)
    fingerprint = store.task_narration_fingerprint(t)

    existing = store.get_task_narration(task_id, project=project)
    if existing and not force:
        # $0 idle-run guard: nothing material changed since the stored narration.
        fresh = existing.get("source_fingerprint") == fingerprint
        age = time.time() - (existing.get("generated_at") or 0)
        if fresh:
            return None
        if age < MIN_INTERVAL and last_cursor <= (existing.get("activity_cursor") or 0):
            return None  # too soon and no new activity

    context = _task_context(t)

    llm = _llm_fn or (lambda ctx: _llm(
        ctx, meta={"source": "narrator", "task_id": task_id, "project": project}))
    narration = llm(context)
    store.set_task_narration(task_id, narration, last_cursor,
                             source_fingerprint=fingerprint, model=NARRATE_MODEL,
                             project=project)
    return {"task_id": task_id, "narration": narration, "generated_at": time.time(),
            "activity_cursor": last_cursor, "source_fingerprint": fingerprint}


def run_pending(project: str = store.DEFAULT_PROJECT, max_tasks: int = MAX_TASKS,
                _llm_fn=None) -> list:
    """Drain the pending_narrations queue for one project. Applies the trigger-status filter,
    narrates up to max_tasks, and clears each pending marker it processes (narrated or skipped)
    so the queue does not grow. Per-task errors are logged, not raised."""
    triggers = _trigger_statuses()
    results = []
    processed = 0
    for row in store.list_pending_narrations(project=project):
        if processed >= max_tasks:
            break
        task_id = row["task_id"]
        reason = (row.get("reason") or "").lower()
        status = (row.get("status") or "").strip().lower()
        # Trigger-status filter: 'create' always qualifies; otherwise the new status must be
        # in the configured set. Non-qualifying markers are dropped without an LLM call.
        if triggers is not None and reason != "create" and status not in triggers:
            store.clear_pending_narration(task_id, project=project)
            continue
        try:
            r = narrate_task(task_id, project=project, _llm_fn=_llm_fn)
            if r:
                results.append(r)
        except Exception as e:
            print(f"narrate {task_id}: {e}", flush=True)
            continue  # leave the marker so a later cycle retries
        store.clear_pending_narration(task_id, project=project)
        processed += 1
    return results


# --- NARRATE-3: deliverable CEO-voice header (rewrites the structured brief) ---

def narrate_deliverable(project: str, deliverable_id: str, force: bool = False,
                        _llm_fn=None) -> Optional[dict]:
    """Rewrite a deliverable's structured mission brief into a 3-4 sentence CEO header.

    Grounds the LLM on mission_narrative.build_mission_brief (no raw-data invention) and keys
    freshness off brief_source_fingerprint, so a burst of linked-task changes collapses into one
    regeneration and an unchanged deliverable makes zero API calls. Returns None when skipped."""
    import mission_narrative

    status = store.get_mission_status(project=project, deliverable_id=deliverable_id)
    if status.get("error"):
        return None
    fingerprint = mission_narrative.brief_source_fingerprint(status)

    deliverable = store.get_deliverable(deliverable_id, project=project) or {}
    metadata = deliverable.get("metadata") or {}
    if not force and metadata.get("ceo_narrative_fingerprint") == fingerprint \
            and metadata.get("ceo_narrative"):
        return None  # $0 idle-run guard: nothing material changed

    activity = store._deliverable_activity(project, deliverable_id)
    brief = mission_narrative.build_mission_brief(status, recent_activity=activity)
    context = (brief.get("summary_markdown") or "")
    honesty = brief.get("honesty_note")
    if honesty:
        context = f"{context}\n\n{honesty}"

    llm = _llm_fn or (lambda ctx: _llm(
        ctx, _DELIVERABLE_SYSTEM, meta={"source": "narrator", "project": project}))
    narration = llm(context)
    store.set_deliverable_narration(deliverable_id, narration, source_fingerprint=fingerprint,
                                    model=NARRATE_MODEL, project=project)
    return {"deliverable_id": deliverable_id, "narration": narration,
            "source_fingerprint": fingerprint}


def run_deliverables(project: str = store.DEFAULT_PROJECT, max_deliverables: int = MAX_TASKS,
                     _llm_fn=None) -> list:
    """Re-narrate every deliverable in the project whose brief fingerprint has moved. Each call
    self-skips when unchanged, so this is safe to run every drain cycle. Errors are logged."""
    results = []
    for deliverable in store.list_deliverables(project=project)[:max_deliverables]:
        did = deliverable.get("id")
        if not did:
            continue
        try:
            r = narrate_deliverable(project, did, _llm_fn=_llm_fn)
            if r:
                results.append(r)
        except Exception as e:
            print(f"narrate deliverable {did}: {e}", flush=True)
    return results


if __name__ == "__main__":
    from pathlib import Path
    _env = Path(__file__).parent / ".env"
    if _env.exists():
        for _line in _env.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

    task_arg = sys.argv[1] if len(sys.argv) > 1 else None
    proj_arg = sys.argv[2] if len(sys.argv) > 2 else store.DEFAULT_PROJECT
    store.init_db(proj_arg)
    if task_arg:
        r = narrate_task(task_arg, project=proj_arg, force=True)
        print(json.dumps(r, indent=2) if r else "skipped (no such task or nothing changed)")
    else:
        res = run_pending(project=proj_arg)
        print(f"narrated {len(res)} task(s) for project '{proj_arg}'")
        for r in res:
            print(f"  {r['task_id']}: {r['narration'][:80]}...")
