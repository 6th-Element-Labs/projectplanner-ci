#!/usr/bin/env python3
"""Haiku epistemic summarizer — Phase MA-4 (see docs/MULTI_AGENT_COORDINATION.md).

Background job: for each task that has activity newer than its last summary cursor,
calls a cheap LLM to compress the activity trail into a ≤50-word rationale. Stored
in task_summaries; returned by store.get_task as the 'rationale' field so agents
get pre-digested context from get_task without reading comments/ADRs/git logs.

Model: PM_SUMMARIZE_MODEL (default: taikun-summarize = gpt-4o-mini, a cheap non-reasoning
model that respects the 120-token output cap; ~50-100x cheaper than taikun-chat/gpt-5.5).
Override to taikun-haiku once ANTHROPIC_API_KEY is configured — see deploy/gateway/config.yaml.

Run via: python jobs.py summarize_pending
Or directly: python summarize.py [task_id [project]]  (for one-shot / debugging)
"""
import json
import os
import sys
import time
from typing import Optional

import httpx

import store

BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
SUMMARIZE_MODEL = os.environ.get("PM_SUMMARIZE_MODEL", "taikun-summarize")  # cheap gpt-4o-mini
MIN_INTERVAL = int(os.environ.get("PM_SUMMARIZE_INTERVAL", "900"))  # seconds between re-runs per task
MAX_TOKENS = 120   # enough for ~50 words with some headroom

_SYSTEM = (
    "You are a planning assistant. Compress the following task activity into ONE paragraph "
    "of at most 50 words. Include: current state, any key decisions or constraints, what it "
    "is blocked on (if anything), and what happens next. Be precise and factual. No filler, "
    "no headers, no bullet points. Output ONLY the paragraph."
)


def _llm(task_context: str) -> str:
    r = httpx.post(
        f"{BASE}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"model": SUMMARIZE_MODEL,
              "messages": [{"role": "system", "content": _SYSTEM},
                           {"role": "user", "content": task_context}],
              "max_tokens": MAX_TOKENS},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _activity_text(activity: list) -> str:
    lines = []
    for a in activity[-20:]:
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


def summarize_task(task_id: str, project: str = store.DEFAULT_PROJECT,
                   _llm_fn=None) -> Optional[dict]:
    """Summarize one task. Returns the summary dict or None if skipped/no activity.
    _llm_fn is injectable for testing (replaces the real HTTP call)."""
    t = store.get_task(task_id, project=project)
    if not t:
        return None

    activity = t.get("activity") or []
    if not activity:
        return None

    # Skip if summarized recently — min_interval not yet elapsed
    existing = store.get_task_summary(task_id, project=project)
    if existing:
        age = time.time() - (existing.get("generated_at") or 0)
        max_cursor = max((a.get("id", 0) for a in activity), default=0)
        if age < MIN_INTERVAL and max_cursor <= existing.get("activity_cursor", 0):
            return existing  # nothing new, too soon

    activity_text = _activity_text(activity)
    if not activity_text:
        return None

    last_cursor = max((a.get("id", 0) for a in activity), default=0)

    task_context = (
        f"Task: {task_id} — {t.get('title', '')}\n"
        f"Status: {t.get('status', '')}\n"
        f"Depends on: {', '.join(t.get('depends_on') or []) or 'nothing'}\n"
        f"Description: {(t.get('description') or '')[:400]}\n\n"
        f"Recent activity ({len(activity)} entries, showing last 20):\n"
        f"{activity_text}"
    )

    llm = _llm_fn or _llm
    rationale = llm(task_context)
    store.set_task_summary(task_id, rationale, last_cursor, project=project)
    return {"task_id": task_id, "rationale": rationale,
            "generated_at": time.time(), "activity_cursor": last_cursor}


def run_pending(project: str = store.DEFAULT_PROJECT, max_tasks: int = 20,
                _llm_fn=None) -> list:
    """Find all tasks with new activity since their last summary and summarize them.
    Returns list of summary dicts. Errors on individual tasks are logged, not raised."""
    pending = store.get_tasks_needing_summary(project=project, min_interval=MIN_INTERVAL)
    results = []
    for task_id in pending[:max_tasks]:
        try:
            r = summarize_task(task_id, project=project, _llm_fn=_llm_fn)
            if r:
                results.append(r)
        except Exception as e:
            print(f"summarize {task_id}: {e}", flush=True)
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
        r = summarize_task(task_arg, project=proj_arg)
        print(json.dumps(r, indent=2) if r else "skipped (no activity or too soon)")
    else:
        results = run_pending(project=proj_arg)
        print(f"summarized {len(results)} task(s) for project '{proj_arg}'")
        for r in results:
            print(f"  {r['task_id']}: {r['rationale'][:80]}...")
