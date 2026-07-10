"""Weekly digest (Phase 3.5 — see docs/AGENT_ROADMAP.md).

Chief-of-staff brief = plan signals + activity deltas since the last digest, written
by the LLM in one call (no tools). Posted as a board entry (the `digests` table) and
shown in the Pulse tab. No email yet — that's Phase 4 (Slack + Gmail). The scheduler
(Phase 4) will call generate_digest() on a timer; for now it's on-demand.
"""
import json
import os
import time

import httpx

import signals
import store

BASE = os.environ.get("PM_LLM_BASE_URL", "http://127.0.0.1:8095/v1")
KEY = os.environ.get("PM_LLM_KEY") or os.environ.get("LLM_GATEWAY_MASTER_KEY", "")
CHAT_MODEL = os.environ.get("PM_LLM_CHAT_MODEL", "taikun-chat")


def _summarize_deltas(acts):
    """Roll the raw activity log since `ts` into what a brief cares about."""
    created, status_changes, edits, comments = [], [], 0, 0
    for a in acts:
        kind = a.get("kind")
        if kind == "create":
            created.append({"task_id": a["task_id"], "actor": a.get("actor")})
        elif kind == "edit":
            p = a.get("payload") or {}
            if "status" in p:
                status_changes.append({"task_id": a["task_id"], "actor": a.get("actor"), "status": p["status"]})
            else:
                edits += 1
        elif kind in ("comment", "chat"):
            comments += 1
    return {"created": created, "status_changes": status_changes,
            "other_edits": edits, "comments": comments, "total_events": len(acts)}


def _brief(sig, deltas, since_ts):
    today = time.strftime("%Y-%m-%d")
    since = time.strftime("%Y-%m-%d", time.localtime(since_ts)) if since_ts else "the start"
    proj = store.get_meta("project") or "the plan"
    sig_trim = dict(sig)
    for k in ("overdue", "due_soon", "blocked", "ready", "critical_slip"):
        sig_trim[k] = sig_trim[k][:12]
    system = (
        f"You are Maxwell, chief of staff for {proj}. Write a crisp WEEKLY BRIEF for the team lead. "
        f"Today is {today}. Be specific (name task IDs and people), scannable, and short — no fluff. "
        "Use these short markdown sections in order:\n"
        "**Headline** — 1-2 sentences on overall state.\n"
        f"**Changed since {since}** — what moved (created / status changes / activity); if nothing, say so.\n"
        "**Slipping** — overdue + critical-path slips, most urgent first.\n"
        "**Ready to start** — newly actionable tasks worth picking up.\n"
        "**Needs attention** — blockers and past-due decisions.\n"
        "Keep the whole brief under ~250 words."
    )
    user = f"SIGNALS:\n{json.dumps(sig_trim)}\n\nCHANGES SINCE {since}:\n{json.dumps(deltas)}"
    body = {"model": CHAT_MODEL, "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": user}],
            # UI-12: gateway callback attributes this call's spend to the digest job.
            "metadata": {"source": "digest"}}
    r = httpx.post(f"{BASE}/chat/completions", headers={"Authorization": f"Bearer {KEY}"}, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"].get("content") or ""


def generate_digest(since_ts=None):
    """Compute signals + deltas, write the brief, persist it. Returns the digest."""
    sig = signals.compute_plan_signals()
    if since_ts is None:
        last = store.last_digest()
        since_ts = last["created_at"] if last else (time.time() - 7 * 86400)
    deltas = _summarize_deltas(store.activity_since(since_ts))
    content = _brief(sig, deltas, since_ts)
    did = store.add_digest(since_ts, content, {"counts": sig["counts"], "deltas": deltas})
    return {"id": did, "content": content, "counts": sig["counts"],
            "deltas": deltas, "since_ts": since_ts, "created_at": time.time()}
