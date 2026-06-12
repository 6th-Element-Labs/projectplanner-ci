"""Ingest + triage core (Phase 5 — see docs/AGENT_ROADMAP.md).

The shared pipeline behind #5 (draft-from-live-state) and #9 (Live Inbox): any inbound
artifact — a pasted transcript, an uploaded document, later a polled email or Slack
message — is (1) ingested into the incremental RAG corpus (so it's citable) and (2)
triaged by the agent against the plan, which proposes the implied task changes
(propose-to-confirm). The SOURCE is just transport; this is the one core both features ride.
"""
import agent
import rag


def ingest_and_triage(kind, title, text, ingest=True, applied_mode=False):
    kind = (kind or "note").strip()
    title = (title or "").strip()
    label = (f"{kind}: {title}" if title else kind)
    chunks = rag.add_document(kind, label, text) if (ingest and (text or "").strip()) else 0
    result = agent.triage(kind, title, text or "", applied_mode=applied_mode)
    return {
        "summary": result.get("answer"),
        "proposals": result.get("proposals", []),
        "new_tasks": result.get("new_tasks", []),
        "sources": result.get("sources", []),
        "ingested_chunks": chunks,
        "rag_label": label,
    }
