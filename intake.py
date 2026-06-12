"""Ingest + triage core (Phase 5 — see docs/AGENT_ROADMAP.md).

The shared pipeline behind #5 (draft-from-live-state) and #9 (Live Inbox): any inbound
artifact — a pasted transcript, an uploaded document, later a polled email or Slack
message — is (1) ingested into the incremental RAG corpus (so it's citable) and (2)
triaged by the agent against the plan, which proposes the implied task changes
(propose-to-confirm). The SOURCE is just transport; this is the one core both features ride.
"""
import logging

import agent
import rag
import scrub

log = logging.getLogger("intake")


def ingest_and_triage(kind, title, text, ingest=True, applied_mode=False):
    kind = (kind or "note").strip()
    title = (title or "").strip()
    label = (f"{kind}: {title}" if title else kind)
    # Fail-and-fix-early: scrub credential VALUES (zip passwords, client secrets, API/
    # subscription keys) BEFORE anything persistent or LLM-facing sees them. The label is
    # kept, so the agent still knows a secret was delivered and can route it — it just
    # never stores or echoes the value.
    text, redacted = scrub.redact(text or "")
    if redacted:
        log.warning("intake: redacted %d secret value(s) from %s before ingest/triage", redacted, label)
    chunks = rag.add_document(kind, label, text) if (ingest and text.strip()) else 0
    result = agent.triage(kind, title, text, applied_mode=applied_mode)
    return {
        "summary": result.get("answer"),
        "proposals": result.get("proposals", []),
        "new_tasks": result.get("new_tasks", []),
        "sources": result.get("sources", []),
        "ingested_chunks": chunks,
        "redacted_secrets": redacted,
        "rag_label": label,
    }
