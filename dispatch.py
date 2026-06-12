"""Dispatch a plan task to the Maxwell Claude Code RUNNER (the push side of the bridge).

The pull side (Claude Code asks the plan what to build via MCP) already exists. This is the
PUSH side: Maxwell hands a ready task to a self-hosted Claude Code runner (on the demo box —
full access to the repo, the VMs, and AWS), which does the work in a `claude/<task>` branch,
pushes it, and hands back a PR compare URL (a human opens the PR — never auto-merged to main).
See docs/AGENT_ROADMAP.md.

Config (.env), from the runner on demo: PM_CC_RUNNER_URL (e.g. http://<demo-ip>:8130),
PM_CC_RUNNER_TOKEN. No-op with a clear reason until both are set.
"""
import os

import httpx

import rag
import store

_REPO = os.environ.get("PM_CC_REPO", "ActionEngine")


def _cfg():
    return ((os.environ.get("PM_CC_RUNNER_URL") or "").strip().rstrip("/"),
            (os.environ.get("PM_CC_RUNNER_TOKEN") or "").strip())


def status():
    url, token = _cfg()
    return {"configured": bool(url and token), "repo": _REPO}


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
    brief = "\n".join([
        f"Development task from the Project Maxwell plan: {task_id} — {t.get('title')}",
        "",
        f"Workstream: {t.get('_wsName')} ({t.get('_wsId')})",
        f"Status: {t.get('status')}   Owner: {t.get('owner_person_or_role') or '—'}",
        f"Description: {t.get('description') or '(none)'}",
        f"Entry criteria: {t.get('entry_criteria') or '(none)'}",
        f"Exit criteria (definition of done): {t.get('exit_criteria') or '(none)'}",
        f"Deliverable: {t.get('deliverable') or '(none)'}",
        "",
        "Relevant plan context (from the plan's RAG corpus):",
        ctx,
        "",
        "INSTRUCTIONS:",
        f"- You are already on a fresh `claude/...` branch in the `{_REPO}` repo. Make the code "
        "changes needed to satisfy the exit criteria and COMMIT them to this branch.",
        "- Do NOT push or open a PR yourself — the dispatch wrapper pushes the branch and produces "
        "the PR link.",
        "- Add or adjust tests where it makes sense. If the task is ambiguous, under-specified, or "
        "blocked on something external, make a minimal honest change (or add a short NOTES file) "
        "explaining exactly what's needed, rather than guessing.",
    ])
    return brief, t


def dispatch(task_id, actor="user"):
    url, token = _cfg()
    brief, t = build_brief(task_id)
    if brief is None:
        return {"dispatched": False, "error": "task not found", "task_id": task_id}
    if not (url and token):
        return {"dispatched": False, "disabled": True, "task_id": task_id,
                "reason": "Claude Code runner not configured — set PM_CC_RUNNER_URL + "
                          "PM_CC_RUNNER_TOKEN (from the runner on demo)."}
    try:
        r = httpx.post(url + "/dispatch", json={"task_id": task_id, "brief": brief},
                       headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # surface, don't mask
        return {"dispatched": False, "task_id": task_id, "error": str(e)[:200]}
    job_id = data.get("job_id")
    store.add_comment(task_id, "Maxwell (dispatch)",
                      f"Dispatched to the Claude Code runner (job {job_id}). Building the change on a "
                      f"`claude/{task_id.lower()}` branch; a PR link will follow when it finishes.")
    return {"dispatched": True, "task_id": task_id, "job_id": job_id,
            "status_url": data.get("status_url")}


def job_status(job_id):
    """Fetch a dispatched job's status (running|pushed|no_changes|…) + PR url + log tail."""
    url, token = _cfg()
    if not (url and token):
        return {"error": "runner not configured"}
    try:
        r = httpx.get(f"{url}/job/{job_id}", headers={"Authorization": f"Bearer {token}"}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)[:200]}
