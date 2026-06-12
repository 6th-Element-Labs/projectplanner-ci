"""Live Inbox (Phase 5.5 / autonomous — see docs/AGENT_ROADMAP.md).

The Gmail-poll source (gmail_source.py) feeds each inbound message to `process()`, which
runs the Phase-5 ingest+triage core and — in AUTONOMOUS mode (default) — APPLIES the
implied changes immediately (audited "Maxwell (email)") and EMAILS the sender back
(answers questions, confirms what changed). The Inbox tab is then a LOG of what the agent
did, not a confirm gate. Set PM_INBOX_AUTONOMOUS=0 to fall back to a review queue.
Source-agnostic — takes a simulated email today and Slack later.
"""
import email.utils
import os

import intake
import notify
import store


def _autonomous():
    return (os.environ.get("PM_INBOX_AUTONOMOUS", "1").strip().lower() not in ("0", "false", "no"))


def _compose_reply(summary, applied):
    lines = [summary or "Processed your message."]
    u, c = applied.get("updated", []), applied.get("created", [])
    if u or c:
        lines.append("")
        if u:
            lines.append("Applied — updated: " + ", ".join(u))
        if c:
            lines.append("Applied — created: " + ", ".join(c))
    lines += ["", "— Maxwell · Project Maxwell assistant (plan.taikunai.com)"]
    return "\n".join(lines)


def process(source, external_id, sender, subject, text):
    """Dedupe -> ingest+triage -> (autonomous) apply + reply, else queue. Returns the item."""
    if store.inbox_exists(source, external_id):
        return None
    result = intake.ingest_and_triage("email", subject or source, text, applied_mode=_autonomous())
    triage = {"proposals": result.get("proposals", []), "new_tasks": result.get("new_tasks", []),
              "sources": result.get("sources", [])}
    applied, reply_res, status = {}, None, "pending"
    if _autonomous():
        applied = apply(triage["proposals"], triage["new_tasks"])
        status = "applied"
        addr = email.utils.parseaddr(sender or "")[1]
        if addr:
            try:
                reply_res = notify.reply(addr, f"Re: {subject or 'your message'}",
                                         _compose_reply(result.get("summary"), applied))
            except Exception as e:
                reply_res = {"error": str(e)}
    triage["applied"] = applied
    triage["reply"] = reply_res
    item_id = store.add_inbox_item(source, external_id, sender, subject, result.get("summary"), triage)
    store.set_inbox_status(item_id, status)
    return store.get_inbox_item(item_id)


def apply(proposals, new_tasks):
    """Apply a set of proposals + new tasks. Audited as 'Maxwell (email)'."""
    out = {"updated": [], "created": [], "failed": []}
    for p in (proposals or []):
        tid = p.get("task_id")
        fields = {k: v for k, v in p.items() if k not in ("task_id", "rationale")}
        if tid and fields and store.update_task(tid, fields, actor="Maxwell (email)"):
            out["updated"].append(tid)
        elif tid:
            out["failed"].append(tid)
    for nt in (new_tasks or []):
        body = {k: v for k, v in nt.items() if k != "rationale"}
        t = store.create_task(body, actor="Maxwell (email)")
        (out["created"] if t else out["failed"]).append((t or nt).get("task_id") or nt.get("workstream_id"))
    return out
