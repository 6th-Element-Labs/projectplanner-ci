"""Live Inbox (Phase 5.5 — see docs/AGENT_ROADMAP.md).

The Gmail-poll source (gmail_source.py) feeds each inbound message to `process()`, which
runs the Phase-5 ingest+triage core and parks the result in a review queue (store.inbox)
because nobody is watching when mail arrives. A human clears the queue (confirm/dismiss);
`apply()` applies the confirmed proposals as actor "Maxwell (email)". Source-agnostic — the
same path takes a simulated email today and Slack later.
"""
import intake
import store


def process(source, external_id, sender, subject, text):
    """Dedupe -> ingest+triage -> park in the queue (pending). Returns the queue item or None (dupe)."""
    if store.inbox_exists(source, external_id):
        return None
    result = intake.ingest_and_triage("email", subject or source, text)
    item_id = store.add_inbox_item(
        source, external_id, sender, subject, result.get("summary"),
        {"proposals": result.get("proposals", []),
         "new_tasks": result.get("new_tasks", []),
         "sources": result.get("sources", [])})
    return store.get_inbox_item(item_id)


def apply(proposals, new_tasks):
    """Apply a (possibly human-trimmed) set of proposals + new tasks. Audited as 'Maxwell (email)'."""
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
