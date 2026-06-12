"""Dispatch a plan task to Claude Code to continue development (the push side of the bridge).

The pull side (Claude Code asks the plan what to build via MCP) already exists. This is the
PUSH side: Maxwell hands a ready task to Claude Code, which does the work and opens a PR.

Mechanism (see docs/AGENT_ROADMAP.md): POST the task brief to a Claude Code **Routine**
`/fire` endpoint. That spins up a CLOUD Claude Code session — watchable + steerable in the
desktop/mobile apps (claude.ai/code) — which opens a PR on a `claude/…` branch and NEVER
pushes to main. The `/fire` response returns a session URL we record on the task so the plan
links straight to the live thread.

Config (.env), all from the Routine Steve creates at claude.ai/code/routines:
  PM_CC_ROUTINE_URL    the routine's API-trigger /fire endpoint
  PM_CC_ROUTINE_TOKEN  the bearer token (shown once on creation)
  PM_CC_ROUTINE_BETA   optional beta header (default below)
No-op with a clear message until URL + token are set, so it ships safe before the routine exists.
"""
import os

import httpx

import rag
import store

_DEFAULT_BETA = "experimental-cc-routine-2026-04-01"
_REPO = os.environ.get("PM_CC_REPO", "ActionEngine")


def status():
    return {
        "configured": bool(
            (os.environ.get("PM_CC_ROUTINE_URL") or "").strip()
            and (os.environ.get("PM_CC_ROUTINE_TOKEN") or "").strip()
        ),
        "repo": _REPO,
    }


def build_brief(task_id):
    """A self-contained dev brief: the task's definition-of-done + grounded plan context."""
    t = store.get_task(task_id)
    if not t:
        return None, None
    q = f"{t.get('title') or ''} {t.get('description') or ''}".strip()
    try:
        hits = rag.search(q, top_k=4) if q else []
    except Exception:
        hits = []
    ctx = "\n".join(f"- [{h['file']}] {h['text'][:280]}" for h in hits) or "(none)"
    slug = task_id.lower()
    brief = "\n".join([
        f"Development task from the Project Maxwell plan: **{task_id} — {t.get('title')}**",
        "",
        f"Workstream: {t.get('_wsName')} ({t.get('_wsId')})",
        f"Status: {t.get('status')}   Owner: {t.get('owner_person_or_role') or '—'}",
        f"Description: {t.get('description') or '(none)'}",
        f"Entry criteria: {t.get('entry_criteria') or '(none)'}",
        f"Exit criteria (definition of done): {t.get('exit_criteria') or '(none)'}",
        f"Deliverable: {t.get('deliverable') or '(none)'}",
        "",
        "Relevant plan context (retrieved from the plan's RAG corpus):",
        ctx,
        "",
        "INSTRUCTIONS:",
        f"- Work in the `{_REPO}` repository.",
        f"- Create a branch named `claude/{slug}`. **Never push to `main` or `development`.**",
        "- Implement the task so the exit criteria are met; add or adjust tests where it makes sense.",
        f"- Open a pull request describing the change and referencing plan task {task_id}.",
        "- If the task is ambiguous, under-specified, or blocked on something external, open a"
        " DRAFT PR (or stop and explain) rather than guessing — surface what you need.",
    ])
    return brief, t


def dispatch(task_id, actor="user"):
    """Fire the routine for one task; record the session link back on the task."""
    url = (os.environ.get("PM_CC_ROUTINE_URL") or "").strip()
    token = (os.environ.get("PM_CC_ROUTINE_TOKEN") or "").strip()
    brief, t = build_brief(task_id)
    if brief is None:
        return {"dispatched": False, "error": "task not found", "task_id": task_id}
    if not (url and token):
        return {"dispatched": False, "disabled": True, "task_id": task_id,
                "reason": "Claude Code dispatch not configured — set PM_CC_ROUTINE_URL + "
                          "PM_CC_ROUTINE_TOKEN from a Routine at claude.ai/code/routines."}
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": os.environ.get("PM_CC_ROUTINE_BETA", _DEFAULT_BETA),
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(url, json={"text": brief}, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json() if r.content else {}
    except Exception as e:  # surface, don't mask
        return {"dispatched": False, "task_id": task_id, "error": str(e)[:200]}
    session_url = data.get("claude_code_session_url") or data.get("session_url")
    session_id = data.get("claude_code_session_id") or data.get("session_id")
    note = ("Dispatched to Claude Code to continue development — it will open a PR on a "
            f"`claude/{task_id.lower()}` branch (never main). "
            + (f"Watch the live session: {session_url}" if session_url else "Session created."))
    store.add_comment(task_id, "Maxwell (dispatch)", note)
    return {"dispatched": True, "task_id": task_id, "session_id": session_id,
            "session_url": session_url}
