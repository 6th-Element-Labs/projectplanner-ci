#!/usr/bin/env python3
"""MCP server for the Project Maxwell plan (Phase 1.5 — see docs/AGENT_ROADMAP.md).

A second front door over the SAME primitives the web agent uses: read tasks/docs,
ask the plan agent, and create/update tasks — from Cursor, Claude Desktop, Claude
Code, etc. Runs as its own process (Streamable HTTP on 127.0.0.1:8111); Caddy routes
https://plan.taikunai.com/mcp here. Reuses store/rag/agent in-process and shares the
SQLite file (WAL) with the web app.

Auth: reads are open. Writes are open too UNLESS PM_MCP_TOKEN is set, in which case
the write tools require `Authorization: Bearer <PM_MCP_TOKEN>`. This matches the
public web API today; tighten when real login lands.
"""
import json
import os

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import agent
import digest as digest_mod
import intake as intake_mod
import notify as notify_mod
import rag
import signals
import store

store.init_db()  # self-sufficient: the web app normally creates the schema

_PORT = int(os.environ.get("PM_MCP_PORT", "8111"))
_PUBLIC_HOST = (os.environ.get("PM_MCP_PUBLIC_HOST") or "plan.taikunai.com").strip()
# We sit behind Caddy (TLS), which forwards Host: <public host>. MCP's DNS-rebinding
# protection rejects unknown Hosts (421), so trust the public host + the local bind.
_SECURITY = TransportSecuritySettings(
    allowed_hosts=[_PUBLIC_HOST, f"127.0.0.1:{_PORT}", f"localhost:{_PORT}", "127.0.0.1", "localhost"],
    allowed_origins=[f"https://{_PUBLIC_HOST}", f"http://127.0.0.1:{_PORT}"],
)

mcp = FastMCP(
    "taikun-plan",
    instructions=(
        "Project Maxwell (TEEP Barnett) Phase-1 pilot plan. Use ask_plan for a reasoned, "
        "doc-grounded answer about the whole plan; search_tasks/get_task to read tasks; "
        "board_summary for the at-a-glance board; doc_search for the plan docs; and "
        "create_task/update_task/add_comment to change the plan."
    ),
    host="127.0.0.1",
    port=_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=_SECURITY,
)


def _require_write(ctx):
    """Gate writes when PM_MCP_TOKEN is set; open otherwise (matches the public web API)."""
    token = (os.environ.get("PM_MCP_TOKEN") or "").strip()
    if not token:
        return
    auth = ""
    try:
        auth = ctx.request_context.request.headers.get("authorization", "") or ""
    except Exception:
        auth = ""
    if auth.replace("Bearer ", "").strip() != token:
        raise ValueError("unauthorized: provide Authorization: Bearer <PM_MCP_TOKEN>")


# ---- read tools (open) ---------------------------------------------------
@mcp.tool()
def search_tasks(workstream: str = "", status: str = "", owner_person: str = "",
                 blocking: bool = False, query: str = "") -> str:
    """Filter the live plan's tasks. All args optional: workstream id (SSO/SEN/BEDROCK/...),
    status (Not Started|In Progress|Blocked|Done), owner_person substring, blocking, free-text
    query. Returns a JSON list of {task_id,title,status,owner_person_or_role,workstream,...}."""
    return json.dumps(agent._search_tasks({
        "workstream": workstream, "status": status, "owner_person": owner_person,
        "blocking": blocking, "query": query}))


@mcp.tool()
def get_task(task_id: str) -> str:
    """Full detail of one task: description, all fields, dependencies, and recent activity."""
    t = store.get_task(task_id)
    return json.dumps(agent._task_brief(t, full=True)) if t else "no such task"


@mcp.tool()
def board_summary() -> str:
    """Whole-board summary: project name + rollups, then one line per task."""
    return (f"Project: {store.get_meta('project')}\n"
            f"Rollups: {json.dumps(store.get_meta('rollups') or {})}\n\n"
            f"{agent.board_summary_text()}")


@mcp.tool()
def doc_search(query: str) -> str:
    """Search the plan docs (PRD, architecture, integrations, security, the full plan).
    Returns cited snippets: [{file, text}]."""
    hits = rag.search(query, top_k=5)
    return json.dumps([{"file": h["file"], "text": h["text"]} for h in hits]) if hits else "no matches"


@mcp.tool()
def get_plan_signals() -> str:
    """Derived plan health: counts + overdue/due-soon/blocked/ready tasks, critical-path slips,
    past-due decisions, and each owner's next-best 1-2 tasks. Use for 'what's slipping?' or digests."""
    return json.dumps(signals.compute_plan_signals())


@mcp.tool()
def ask_plan(question: str) -> str:
    """Ask the plan-wide Maxwell agent. Returns a reasoned, doc-grounded answer (with sources)
    and, when relevant, a proposed task change (NOT applied — call update_task to apply it)."""
    r = agent.run(None, question)
    return json.dumps({"answer": r.get("answer"), "sources": r.get("sources"),
                       "proposed_change": r.get("proposal")})


# ---- write tools (gated by PM_MCP_TOKEN when set) ------------------------
@mcp.tool()
def update_task(task_id: str, ctx: Context, title: str = "", description: str = "", status: str = "",
                owner_org: str = "", owner_person_or_role: str = "", assignee: str = "",
                phase: str = "", start_date: str = "", finish_date: str = "",
                risk_level: str = "", is_blocking: str = "") -> str:
    """Update only the fields you pass on a task. status: Not Started|In Progress|Blocked|Done;
    dates: YYYY-MM-DD; is_blocking: 'true'/'false'. Audited as actor 'MCP'."""
    _require_write(ctx)
    fields = {}
    for k, v in (("title", title), ("description", description), ("status", status),
                 ("owner_org", owner_org), ("owner_person_or_role", owner_person_or_role),
                 ("assignee", assignee), ("phase", phase), ("start_date", start_date),
                 ("finish_date", finish_date), ("risk_level", risk_level)):
        if v != "":
            fields[k] = v
    if is_blocking != "":
        fields["is_blocking"] = is_blocking.strip().lower() in ("1", "true", "yes")
    if not fields:
        return "no fields to update"
    t = store.update_task(task_id, fields, actor="MCP")
    return json.dumps(agent._task_brief(t)) if t else "no such task"


@mcp.tool()
def create_task(workstream_id: str, title: str, ctx: Context, description: str = "",
                owner_org: str = "", owner_person_or_role: str = "", status: str = "",
                phase: str = "", risk_level: str = "") -> str:
    """Create a task in a workstream (SSO/SEN/BEDROCK/...). Returns the created task. Actor 'MCP'."""
    _require_write(ctx)
    data = {"workstream_id": workstream_id, "title": title, "description": description or None,
            "owner_org": owner_org or None, "owner_person_or_role": owner_person_or_role or None,
            "status": status or None, "phase": phase or None, "risk_level": risk_level or None}
    t = store.create_task(data, actor="MCP")
    return json.dumps(agent._task_brief(t)) if t else "workstream_id and title required"


@mcp.tool()
def add_comment(task_id: str, text: str, ctx: Context) -> str:
    """Add a note to a task's activity log (audited as actor 'MCP')."""
    _require_write(ctx)
    t = store.add_comment(task_id, "MCP", text)
    return "ok" if t else "no such task"


@mcp.tool()
def generate_digest(ctx: Context) -> str:
    """Generate + post the weekly chief-of-staff brief (plan signals + activity deltas since the
    last digest). Returns the brief text. Creates a digest record."""
    _require_write(ctx)
    return digest_mod.generate_digest().get("content", "")


@mcp.tool()
def notify(subject: str, text: str, ctx: Context) -> str:
    """Send a message to the wired channels (Slack + Email). Unconfigured channels are dry-run."""
    _require_write(ctx)
    return json.dumps(notify_mod.send(subject, text))


@mcp.tool()
def ingest_and_triage(kind: str, title: str, text: str, ctx: Context) -> str:
    """Ingest an artifact (email / transcript / document / note) into the RAG corpus AND triage it
    against the plan. Returns {summary, proposals, new_tasks, sources} — proposals are NOT applied
    (use update_task / create_task to apply). kind: email|transcript|document|note."""
    _require_write(ctx)
    return json.dumps(intake_mod.ingest_and_triage(kind, title, text))


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
