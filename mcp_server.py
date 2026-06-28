#!/usr/bin/env python3
"""MCP server for the Project Maxwell plan (Phase 1.5 — see docs/AGENT_ROADMAP.md).

A second front door over the SAME primitives the web agent uses: read tasks/docs,
ask the plan agent, and create/update tasks — from Cursor, Claude Desktop, Claude
Code, etc. Runs as its own process (Streamable HTTP on 127.0.0.1:8111); Caddy routes
https://plan.taikunai.com/mcp here. Reuses store/rag/agent in-process and shares the
SQLite file (WAL) with the web app.

Auth: reads are open. Writes use the shared Switchboard bearer-principal path when
PM_AUTH_MODE=required. Existing PM_MCP_TOKEN deployments keep working as an env-token
principal; explicit principals can be created in the SQLite store.
"""
import json
import os

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import agent
import auth
import digest as digest_mod
import intake as intake_mod
import notify as notify_mod
import rag
import signals
import store

for _pid in store.PROJECTS:  # ensure every project's schema exists (the web app normally seeds them)
    store.init_db(_pid)

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
        "Multi-project planning board. Every task/board tool takes a `project` arg: 'maxwell' "
        "(default — TEEP Barnett Phase-1 pilot) or 'helm' (the Helm marine-chartplotter build). "
        "ALWAYS pass project='helm' to read or update Helm tasks (workstreams ENGINE/CHART/CONTRACT/"
        "OWNSHIP/ROUTE/AIS/ALARM/WX/...); omit it (or 'maxwell') for the Maxwell plan. Writes go ONLY "
        "to the named board — they can never cross. Use search_tasks/get_task to read, board_summary "
        "for the at-a-glance board, get_plan_signals for health, and create_task/update_task/add_comment "
        "to change a plan. ask_plan also takes project (Helm answers are board-grounded incl. "
        "code-audit comments); doc_search remains Maxwell-only."
    ),
    host="127.0.0.1",
    port=_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=_SECURITY,
)


def _dumps(obj) -> str:
    """json.dumps with sort_keys=True — deterministic serialization for prompt-cache hits.
    Stable key order means identical responses share a cache hit across agent sessions."""
    return json.dumps(obj, sort_keys=True)


def _require_write(ctx, project: str = "maxwell", scopes=("write:tasks",)):
    """Gate writes through the shared Switchboard bearer-principal path."""
    try:
        return auth.authenticate(project, auth.bearer_from_mcp_context(ctx),
                                 scopes, dev_actor="MCP")
    except PermissionError as e:
        raise ValueError(str(e))


def _dep_ids(s):
    """Parse a comma/space/newline-separated list of task ids into a deduped, upper-cased list."""
    out, seen = [], set()
    for tok in (s or "").replace("\n", ",").replace(" ", ",").split(","):
        t = tok.strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _unknown_ids(ids, project):
    """Task ids that don't exist on the project. A dependency to a non-existent task is a broken
    graph edge (invalid input) — callers REJECT it rather than write a dangling reference that would
    spread into every audit that traverses the graph."""
    return [d for d in ids if not store.get_task(d, project=project)]


# ---- read tools (open) ---------------------------------------------------
@mcp.tool()
def search_tasks(workstream: str = "", status: str = "", owner_person: str = "",
                 blocking: bool = False, query: str = "", project: str = "maxwell") -> str:
    """Filter a plan's tasks. project selects the board ('maxwell' default, or 'helm'). All other
    args optional: workstream id (SSO/SEN/... for Maxwell; ENGINE/CHART/... for Helm), status
    (Not Started|In Progress|Blocked|Done), owner_person substring, blocking, free-text query.
    Returns a JSON list of {task_id,title,status,owner_person_or_role,workstream,...}."""
    return _dumps(agent._search_tasks({
        "workstream": workstream, "status": status, "owner_person": owner_person,
        "blocking": blocking, "query": query}, project=project))


@mcp.tool()
def get_task(task_id: str, project: str = "maxwell") -> str:
    """Full detail of one task: description, all fields, dependencies, and recent activity.
    project selects the board ('maxwell' default, or 'helm')."""
    t = store.get_task(task_id, project=project)
    return _dumps(agent._task_brief(t, full=True)) if t else "no such task"


@mcp.tool()
def board_summary(project: str = "maxwell") -> str:
    """Full board snapshot: project name + rollups, then one line per task.
    Use ONCE at session start for orientation. For recurring 'has anything changed?' checks
    use get_lane_delta instead — it returns only what changed and costs ~50 tokens when nothing
    did vs ~3000-5000 tokens here. project selects the board ('maxwell' default, or 'helm')."""
    return (f"Project: {store.get_meta('project', project=project)}\n"
            f"Rollups: {_dumps(store.get_meta('rollups', project=project) or {})}\n\n"
            f"{agent.board_summary_text(project=project)}")


@mcp.tool()
def get_lane_delta(project: str = "maxwell", lane: str = "", since_cursor: int = 0) -> str:
    """Efficient poll replacement — returns ONLY tasks that changed since your last call.
    Use this instead of board_summary in any polling loop. Costs ~50 tokens when nothing
    changed (empty updates list) vs 3000-5000 tokens for a full board_summary.

    project: 'maxwell' or 'helm'. lane: workstream id to filter (e.g. 'ENGINE', 'CHART',
    'OWNSHIP') — leave blank for all workstreams. since_cursor: the cursor value from your
    last response; pass 0 on first call.

    Returns {cursor, updates: [{task_id, status, title, workstream_id, kinds}]}.
    Save the returned cursor and pass it on your next call. kinds lists the activity types
    that occurred (edit, comment, create). Call get_task for full detail on any changed task."""
    return _dumps(store.get_activity_delta(since_cursor=since_cursor, lane=lane, project=project))


@mcp.tool()
def doc_search(query: str) -> str:
    """Search the plan docs (PRD, architecture, integrations, security, the full plan).
    Returns cited snippets: [{file, text}]."""
    hits = rag.search(query, top_k=5)
    return _dumps([{"file": h["file"], "text": h["text"]} for h in hits]) if hits else "no matches"


@mcp.tool()
def get_plan_signals(project: str = "maxwell") -> str:
    """Derived plan health: counts + overdue/due-soon/blocked/ready tasks, critical-path slips,
    past-due decisions, and each owner's next-best 1-2 tasks. Use for 'what's slipping?' or digests.
    project selects the board ('maxwell' default, or 'helm')."""
    return _dumps(signals.compute_plan_signals(project=project))


@mcp.tool()
def get_working_agreement(project: str = "maxwell") -> str:
    """Connect-time policy for agents: definition of done, branch convention, merge strategy,
    canonical main SHA, and the session-start sequence. Call before register_agent."""
    return _dumps(store.get_working_agreement(project=project))


@mcp.tool()
def ask_plan(question: str, project: str = "maxwell") -> str:
    """Ask the plan-wide agent a question about a board. project selects it ('maxwell' default, or
    'helm'). For 'helm' the answer is grounded in the live board (incl. code-audit comments); for
    'maxwell' it also grounds in the plan docs via RAG. Returns a reasoned answer (+ sources) and,
    when relevant, a proposed task change (NOT applied — call update_task to apply it)."""
    r = agent.run(None, question, project=project)
    return _dumps({"answer": r.get("answer"), "sources": r.get("sources"),
                   "proposed_change": r.get("proposal")})


# ---- file lease tools (open — advisory, no token required) ---------------
@mcp.tool()
def claim_files(agent_id: str, files: str, ctx: Context, project: str = "maxwell",
                task_id: str = "", ttl_minutes: int = 30) -> str:
    """Claim file paths before editing them (advisory soft lock — prevents parallel agents
    from clobbering each other). Call before writing; call release_files when done.

    agent_id: a stable string identifying this agent session (e.g. 'claude/ENGINE-11').
    files: comma or newline-separated list of paths (relative to repo root).
    task_id: the board task you're working on (optional but recommended).
    ttl_minutes: auto-expire after this many minutes if release_files is never called (default 30).

    On success: {lease_id, files, expires_at, ttl_minutes}
    On conflict: {conflict, task_id, files, retry_after_seconds} — use Bash(sleep N) before retrying."""
    file_list = [f.strip() for f in files.replace("\n", ",").split(",") if f.strip()]
    if not file_list:
        return _dumps({"error": "no files given"})
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.claim_files(agent_id, file_list,
                                    task_id=task_id or None,
                                    ttl_minutes=max(1, ttl_minutes),
                                    project=project))


@mcp.tool()
def release_files(lease_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Release a file lease when you are done editing. Pass the lease_id returned by
    claim_files. Idempotent — releasing an already-released lease returns an error but does
    not corrupt state. project selects the board ('maxwell' default, or 'helm')."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.release_files(lease_id, project=project))


@mcp.tool()
def check_files(files: str, project: str = "maxwell") -> str:
    """Check whether any of the given file paths are held by an active lease.
    files: comma or newline-separated list of paths.
    Returns a list of {file, held_by, task_id, expires_at} for files that ARE held.
    Empty list means all files are free — safe to edit without claiming first (though
    calling claim_files is strongly preferred to avoid races).
    project selects the board ('maxwell' default, or 'helm')."""
    file_list = [f.strip() for f in files.replace("\n", ",").split(",") if f.strip()]
    if not file_list:
        return _dumps([])
    return _dumps(store.check_files(file_list, project=project))


@mcp.tool()
def list_active_leases(project: str = "maxwell") -> str:
    """All active file leases on the board — who holds what, and when it expires.
    Use to see which agents are currently active and which files they have claimed.
    Expired and released leases are not shown.
    project selects the board ('maxwell' default, or 'helm')."""
    return _dumps(store.list_active_leases(project=project))


# ---- IXP-core runtime lifecycle -----------------------------------------
@mcp.tool()
def register_agent(agent_id: str, runtime: str, ctx: Context, model: str = "",
                   lane: str = "", task_id: str = "", ttl_s: int = 120,
                   control_json: str = "{}", project: str = "maxwell") -> str:
    """Register a live agent session. Call at session start before claiming work.
    control_json advertises truthful control fidelity, e.g. {"mode":"advisory_poll"}."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        control = json.loads(control_json or "{}")
    except Exception:
        return _dumps({"error": "control_json must be a JSON object string"})
    return _dumps(store.register_agent(
        agent_id=agent_id, runtime=runtime, model=model, lane=lane, task_id=task_id,
        ttl_s=ttl_s, control=control, principal_id=principal["id"],
        actor=auth.actor(principal), project=project))


@mcp.tool()
def heartbeat(agent_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Renew presence for a registered agent session."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.heartbeat(agent_id, actor=auth.actor(principal), project=project))


@mcp.tool()
def list_active_agents(project: str = "maxwell", lane: str = "") -> str:
    """List active registered agents and their advertised control fidelity."""
    return _dumps(store.list_active_agents(lane=lane, project=project))


@mcp.tool()
def claim_resource(agent_id: str, resource_type: str, names: str, ctx: Context,
                   task_id: str = "", ttl_seconds: int = 1800,
                   idem_key: str = "", project: str = "maxwell") -> str:
    """Generic IXP resource claim. resource_type can be file, port, build_dir, worktree,
    binary, branch, task, etc. names is comma/newline-separated."""
    principal = _require_write(ctx, project, ("write:ixp",))
    name_list = [n.strip() for n in names.replace("\n", ",").split(",") if n.strip()]
    return _dumps(store.claim_resources(
        agent_id=agent_id, resource_type=resource_type, names=name_list,
        task_id=task_id or None, ttl_seconds=ttl_seconds, principal_id=principal["id"],
        actor=auth.actor(principal), idem_key=idem_key, project=project))


@mcp.tool()
def check_resource(resource_type: str, names: str, project: str = "maxwell") -> str:
    """Check whether generic IXP resources are held by active leases."""
    name_list = [n.strip() for n in names.replace("\n", ",").split(",") if n.strip()]
    return _dumps(store.check_resources(resource_type, name_list, project=project))


@mcp.tool()
def release_resource(lease_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Release a generic IXP resource lease."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.release_resource_lease(
        lease_id, actor=auth.actor(principal), project=project))


@mcp.tool()
def list_active_resource_leases(project: str = "maxwell") -> str:
    """All active generic IXP resource leases."""
    return _dumps(store.list_active_resource_leases(project=project))


@mcp.tool()
def claim_next(agent_id: str, ctx: Context, lanes: str = "", capabilities: str = "",
               max_risk: str = "", max_budget_usd: float = 0.0,
               ttl_seconds: int = 1800, idem_key: str = "",
               project: str = "maxwell") -> str:
    """Atomically claim the next unblocked task for this agent. This is the first +TXP
    scheduler primitive: dependency-aware, idempotent, and returns budget/model guidance."""
    principal = _require_write(ctx, project, ("write:ixp",))
    lane_list = [x.strip().upper() for x in lanes.replace("\n", ",").split(",") if x.strip()]
    cap_list = [x.strip() for x in capabilities.replace("\n", ",").split(",") if x.strip()]
    return _dumps(store.claim_next(
        agent_id=agent_id, lanes=lane_list, capabilities=cap_list,
        max_risk=max_risk, max_budget_usd=max_budget_usd or None,
        principal_id=principal["id"], actor=auth.actor(principal),
        ttl_seconds=ttl_seconds, idem_key=idem_key, project=project))


@mcp.tool()
def complete_claim(claim_id: str, ctx: Context, evidence: str = "",
                   project: str = "maxwell") -> str:
    """Mark a task claim completed, release its task lease, and move the task to In Review.
    evidence should be a JSON object string with branch, head_sha, pr_url/pr_number when known.
    Agents must NOT set Done; the GitHub merge webhook stamps merged_sha and sets Done."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.complete_claim(claim_id, evidence=evidence,
                                      actor=auth.actor(principal), project=project))


@mcp.tool()
def abandon_claim(claim_id: str, reason: str, ctx: Context,
                  project: str = "maxwell") -> str:
    """Abandon a task claim, release its task lease, and return the task to the ready queue."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.abandon_claim(claim_id, reason=reason,
                                     actor=auth.actor(principal), project=project))


@mcp.tool()
def report_usage(ctx: Context, source: str = "agent_report", confidence: str = "reported",
                 task_id: str = "", claim_id: str = "", agent_id: str = "",
                 runtime: str = "", provider: str = "", model: str = "",
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 total_tokens: int = 0, cost_usd: float = 0.0,
                 call_site: str = "coding", request_id: str = "",
                 metadata_json: str = "{}", project: str = "maxwell") -> str:
    """Report usage/cost into Tally. Gateway-measured calls should use source='gateway';
    external coding agents should use source='agent_report' and honest confidence."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        metadata = json.loads(metadata_json or "{}")
    except Exception:
        return _dumps({"error": "metadata_json must be a JSON object string"})
    return _dumps(store.report_usage(
        source=source, confidence=confidence, task_id=task_id or None,
        claim_id=claim_id or None, agent_id=agent_id or None,
        principal_id=principal["id"], runtime=runtime, call_site=call_site,
        provider=provider, model=model, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens or None, cost_usd=cost_usd,
        metadata=metadata, request_id=request_id or None, project=project))


@mcp.tool()
def get_task_tally(task_id: str, project: str = "maxwell") -> str:
    """Tally rollup for one task: spend by source, total tokens/cost, and outcome denominator."""
    return _dumps(store.task_tally(task_id, project=project))


@mcp.tool()
def reconcile(project: str = "maxwell") -> str:
    """Run the local board/git-provenance drift report. This first pass catches board-internal
    contradictions such as Done without merged_sha or In Review without PR/branch evidence."""
    return _dumps(store.reconcile(project=project))


# ---- directed agent IM (IXP write-authenticated) -----------------------
@mcp.tool()
def send_agent_message(from_agent: str, to_agent: str, message: str,
                       ctx: Context, project: str = "maxwell", task_id: str = "",
                       requires_ack: bool = False,
                       ack_deadline_minutes: int = 0,
                       signal: str = "", priority: int = 0,
                       idem_key: str = "") -> str:
    """Send a directed message to another agent session. Unlike add_comment (bulletin
    board, fire-and-forget), this has an ack/read-receipt so the sender can confirm
    the message landed before acting on the assumption it was received.

    from_agent / to_agent: stable agent-session identifiers (e.g. 'claude/ENGINE-11').
    task_id: the task this message is about (optional).
    requires_ack: if true, the receiving agent should call ack_message to confirm receipt.
    ack_deadline_minutes: how long the sender will wait for an ack (0 = no deadline).

    Returns the message record including its id. Pass the id to get_message_status to
    check whether the recipient has acked."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.send_agent_message(
        from_agent, to_agent, message,
        task_id=task_id or None,
        requires_ack=requires_ack,
        ack_deadline_minutes=ack_deadline_minutes or None,
        signal=signal or None,
        priority=priority,
        principal_id=principal["id"],
        idem_key=idem_key,
        project=project,
    ))


@mcp.tool()
def ack_message(message_id: int, ctx: Context, project: str = "maxwell", response: str = "") -> str:
    """Acknowledge a directed message. Call this when you have received and understood a
    message that has requires_ack=true. response is optional — include it to give the
    sender a one-line confirmation (e.g. 'seen — will rebase before merging').
    project selects the board ('maxwell' default, or 'helm')."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.ack_message(message_id, response=response,
                                    actor=auth.actor(principal), project=project))


@mcp.tool()
def list_unacked_messages(to_agent: str, project: str = "maxwell") -> str:
    """Your incoming message inbox — messages directed to you that have not been acked.
    Call at session start and after completing a task to check for coordination messages
    from other agents. to_agent: your agent-session id (e.g. 'claude/CHART-8').
    project selects the board ('maxwell' default, or 'helm')."""
    return _dumps(store.list_unacked_messages(to_agent, project=project))


@mcp.tool()
def get_message_status(message_id: int, project: str = "maxwell") -> str:
    """Check whether a message you sent has been acked. Returns the full message record
    including acked_at and ack_response if the recipient has responded.
    project selects the board ('maxwell' default, or 'helm')."""
    r = store.get_message_status(message_id, project=project)
    return _dumps(r) if r else "message not found"


# ---- blocking dep requests (§2.2 — requires_ack IM with dep semantics) ------
@mcp.tool()
def request_unblock(requesting_agent: str, owner_agent: str,
                    blocking_task_id: str, blocked_task_id: str,
                    message: str, ctx: Context, project: str = "maxwell",
                    ack_deadline_minutes: int = 60) -> str:
    """Ask the agent working on a blocking task to unblock your work. Use this when
    your task has a direct dependency that hasn't been resolved and you need the
    owning agent to act — more urgent and structured than add_comment.

    How it works:
    - Sends a directed, ack-required message to owner_agent.
    - Records the request as 'dep_request' activity on BOTH tasks for the board trail.
    - Returns {request_id, ...}. Poll get_message_status(request_id) to see when the
      owning agent has acked (i.e., picked up and acknowledged the request).

    Fields:
      requesting_agent: your agent-session id ('claude/ROUTE-3')
      owner_agent:      the agent working on the blocker ('claude/ENGINE-11')
      blocking_task_id: the task that is blocking you ('ENGINE-11')
      blocked_task_id:  your task ('ROUTE-3')
      message:          what you need / why it's urgent (1-3 sentences)
      ack_deadline_minutes: how long you'll wait (default 60)
    project: 'maxwell' (default) or 'helm'."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.request_unblock(
        requesting_agent=requesting_agent, blocking_task_id=blocking_task_id,
        blocked_task_id=blocked_task_id, message=message,
        owner_agent=owner_agent, ack_deadline_minutes=ack_deadline_minutes,
        project=project,
    ))


@mcp.tool()
def list_unblock_requests(owner_agent: str, project: str = "maxwell") -> str:
    """Check your queue of unacked blocking dep requests — tasks whose owners are
    waiting on you. Call at session start alongside list_unacked_messages.
    Returns the same structure as list_unacked_messages but filtered to DEP REQUEST
    messages. Ack each with ack_message(request_id, response='unblocked') when done.
    project: 'maxwell' (default) or 'helm'."""
    return _dumps(store.list_unblock_requests(owner_agent, project=project))


# ---- agent state (open — lightweight working-state scratchpad per agent) ----
@mcp.tool()
def set_agent_state(task_id: str, agent_id: str, state: str,
                    ctx: Context, project: str = "maxwell") -> str:
    """Write your working state for a task — a small JSON object (max ~500 chars)
    capturing what you're doing, where you are, and what you plan next. Stored inside
    the task and visible to all agents via get_agent_state. Good keys to include:
      "files_open": which files you have staged or modified
      "next_step": what you're about to do next
      "blocked_on": what you're waiting for (or null)
      "progress": e.g. "3/7 tests passing"
    state: JSON-string object. agent_id: your stable session id (e.g. 'claude/ENGINE-11').
    project: 'maxwell' (default) or 'helm'."""
    try:
        state_obj = json.loads(state)
    except Exception:
        return _dumps({"error": "state must be a valid JSON object string"})
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.set_agent_state(task_id, agent_id, state_obj, project=project))


@mcp.tool()
def get_agent_state(task_id: str, project: str = "maxwell") -> str:
    """Read the working-state blobs for all agents currently on a task.
    Returns {agent_id: {state fields}, ...}. Call this before starting work on a
    task to see if another agent is already active, what files it has open, and what
    it plans next — complements list_unacked_messages for live coordination.
    project: 'maxwell' (default) or 'helm'."""
    return _dumps(store.get_agent_state(task_id, project=project))


# ---- decisions log (open — append-only ADR-lite for multi-agent alignment) --
@mcp.tool()
def record_decision(title: str, context: str, decision: str, rationale: str,
                    author: str, ctx: Context, project: str = "maxwell",
                    task_id: str = "", supersedes: int = 0) -> str:
    """Append an immutable architectural decision record so all agents share settled
    conclusions without re-litigating them. Use this when you've just chosen an
    approach — especially a non-obvious one that another agent might reverse.
    Examples: choosing a library, fixing an interface contract, deciding NOT to do X.

    Fields:
      title:     short (≤80 chars) — 'Use advisory leases, not hard file locks'
      context:   why the decision was needed (1-3 sentences)
      decision:  exactly what was decided (1-3 sentences, present tense)
      rationale: why this option was chosen over the alternatives
      author:    your agent-session id (e.g. 'claude/ENGINE-11')
      task_id:   related task (optional)
      supersedes: id of an earlier decision this replaces (optional, set to 0 if none)

    Decisions are append-only. To reverse one, record a new decision with the old id
    in 'supersedes' — the old record is marked 'superseded' automatically.
    project: 'maxwell' (default) or 'helm'."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.record_decision(
        task_id=task_id or None, author=author or auth.actor(principal), title=title,
        context=context, decision=decision, rationale=rationale,
        supersedes=supersedes or None, project=project,
    ))


@mcp.tool()
def list_decisions(project: str = "maxwell", task_id: str = "",
                   status: str = "") -> str:
    """List architectural decisions recorded by any agent.
    Filter by task_id (decisions about that task) and/or status ('accepted',
    'superseded', 'proposed'). Returns newest-first.
    Check this at session start to know what's already been decided before
    choosing an approach. project: 'maxwell' (default) or 'helm'."""
    return _dumps(store.list_decisions(task_id=task_id or None,
                                      status=status, project=project))


@mcp.tool()
def get_decision(decision_id: int, project: str = "maxwell") -> str:
    """Fetch a single decision record by id. Use when list_decisions refers to a
    decision you want to read in full (context + rationale).
    project: 'maxwell' (default) or 'helm'."""
    r = store.get_decision(decision_id, project=project)
    return _dumps(r) if r else "decision not found"


# ---- task write tools (Switchboard bearer-principal authenticated) -------
@mcp.tool()
def update_task(task_id: str, ctx: Context, title: str = "", description: str = "", status: str = "",
                owner_org: str = "", owner_person_or_role: str = "", assignee: str = "",
                phase: str = "", start_date: str = "", finish_date: str = "",
                risk_level: str = "", is_blocking: str = "", depends_on: str = "",
                project: str = "maxwell") -> str:
    """Update only the fields you pass on a task. status: Not Started|In Progress|Blocked|Done;
    dates: YYYY-MM-DD; is_blocking: 'true'/'false'. depends_on: comma/space-separated task ids that
    REPLACE this task's dependency list (e.g. 'TOOLS-7, SHELL-1'); pass 'none' to clear it (for an
    incremental edge use add_dependency/remove_dependency). Audited as the authenticated actor.
    project selects the board ('maxwell' default, or 'helm') — writes go ONLY to that board."""
    principal = _require_write(ctx, project)
    actor_name = auth.actor(principal)
    fields = {}
    for k, v in (("title", title), ("description", description), ("status", status),
                 ("owner_org", owner_org), ("owner_person_or_role", owner_person_or_role),
                 ("assignee", assignee), ("phase", phase), ("start_date", start_date),
                 ("finish_date", finish_date), ("risk_level", risk_level)):
        if v != "":
            fields[k] = v
    if is_blocking != "":
        fields["is_blocking"] = is_blocking.strip().lower() in ("1", "true", "yes")
    if depends_on != "":
        new_deps = [] if depends_on.strip().lower() in ("none", "clear", "[]") else _dep_ids(depends_on)
        unknown = _unknown_ids(new_deps, project)
        if unknown:   # FAIL LOUD: don't write a dependency to a task that doesn't exist
            return _dumps({"error": "unknown dependency id(s) on project '%s': %s — task NOT updated. "
                           "Create them first or fix the id." % (project, ", ".join(unknown))})
        fields["depends_on"] = new_deps
    if not fields:
        return "no fields to update"
    t = store.update_task(task_id, fields, actor=actor_name, project=project)
    return _dumps(agent._task_brief(t)) if t else "no such task"


@mcp.tool()
def create_task(workstream_id: str, title: str, ctx: Context, description: str = "",
                owner_org: str = "", owner_person_or_role: str = "", status: str = "",
                phase: str = "", risk_level: str = "", depends_on: str = "",
                project: str = "maxwell") -> str:
    """Create a task in a workstream (SSO/SEN/... for Maxwell; ENGINE/CHART/... for Helm). depends_on:
    comma/space-separated task ids this task dependsOn (e.g. 'BOAT-1, WX-10'). Returns the created task.
    Actor 'MCP'. project selects the board ('maxwell' default, or 'helm')."""
    principal = _require_write(ctx, project)
    actor_name = auth.actor(principal)
    deps = _dep_ids(depends_on)
    unknown = _unknown_ids(deps, project)
    if unknown:   # FAIL LOUD: refuse to create a task carrying edges to non-existent tasks
        return _dumps({"error": "unknown dependency id(s) on project '%s': %s — task NOT created. "
                       "Create them first or fix the id." % (project, ", ".join(unknown))})
    data = {"workstream_id": workstream_id, "title": title, "description": description or None,
            "owner_org": owner_org or None, "owner_person_or_role": owner_person_or_role or None,
            "status": status or None, "phase": phase or None, "risk_level": risk_level or None,
            "depends_on": deps}
    t = store.create_task(data, actor=actor_name, project=project)
    return _dumps(agent._task_brief(t)) if t else "workstream_id and title required"


@mcp.tool()
def add_comment(task_id: str, text: str, ctx: Context, project: str = "maxwell") -> str:
    """Add a note to a task's activity log (audited as actor 'MCP').
    project selects the board ('maxwell' default, or 'helm')."""
    principal = _require_write(ctx, project)
    t = store.add_comment(task_id, auth.actor(principal), text, project=project)
    return "ok" if t else "no such task"


@mcp.tool()
def add_dependency(task_id: str, depends_on: str, ctx: Context, project: str = "maxwell") -> str:
    """Add one or more dependency EDGES to a task (task_id dependsOn each id in depends_on,
    comma/space-separated, e.g. 'TOOLS-7, SHELL-1'). APPENDS without clobbering existing deps
    (idempotent, deduped) — use this to wire cross-epic edges. FAIL-FAST: if ANY id is not a real
    task the whole call is REJECTED with an error and nothing is written (a dependency to a
    non-existent task is a broken graph edge) — fix the id or create the target first, then retry.
    project selects the board ('maxwell' default, or 'helm')."""
    principal = _require_write(ctx, project)
    actor_name = auth.actor(principal)
    add = _dep_ids(depends_on)
    if not add:
        return "no dependency ids given"
    t = store.get_task(task_id, project=project)
    if not t:
        return "no such task: " + task_id
    unknown = _unknown_ids(add, project)
    if unknown:   # FAIL LOUD: reject the whole batch — never write a dangling edge
        return _dumps({"error": "unknown task id(s) on project '%s': %s — NO edge added. "
                       "Create the target task(s) first or fix the id." % (project, ", ".join(unknown))})
    merged = list(t.get("depends_on") or [])
    for d in add:
        if d not in merged:
            merged.append(d)
    store.update_task(task_id, {"depends_on": merged}, actor=actor_name, project=project)
    return _dumps({"task_id": task_id, "depends_on": merged})


@mcp.tool()
def remove_dependency(task_id: str, depends_on: str, ctx: Context, project: str = "maxwell") -> str:
    """Remove one or more dependency edges from a task (comma/space-separated ids). Reports which ids
    were actually removed vs not present — a no-op removal is SURFACED, not silently swallowed.
    project selects the board ('maxwell' default, or 'helm')."""
    principal = _require_write(ctx, project)
    actor_name = auth.actor(principal)
    rm = _dep_ids(depends_on)
    if not rm:
        return "no dependency ids given"
    t = store.get_task(task_id, project=project)
    if not t:
        return "no such task: " + task_id
    cur = list(t.get("depends_on") or [])
    rmset = set(rm)
    merged = [d for d in cur if d not in rmset]
    store.update_task(task_id, {"depends_on": merged}, actor=actor_name, project=project)
    res = {"task_id": task_id, "depends_on": merged, "removed": [d for d in cur if d in rmset]}
    not_present = [d for d in rm if d not in cur]
    if not_present:   # surface the no-op rather than pretend it did something
        res["note"] = "not present (nothing to remove): " + ", ".join(not_present)
    return _dumps(res)


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
    return _dumps(notify_mod.send(subject, text))


@mcp.tool()
def dispatch_to_claude_code(task_id: str, ctx: Context) -> str:
    """Push a task to Claude Code to CONTINUE DEVELOPMENT (the autonomous-dev bridge). Builds a
    dev brief (the task's exit criteria + plan-RAG context) and fires a Claude Code cloud session
    that opens a PR on a `claude/<task>` branch — never main — and is watchable in the desktop/
    mobile apps. Returns {dispatched, session_url, ...}. Records the session link on the task.
    No-op with a clear reason until the routine is configured on the plan host."""
    principal = _require_write(ctx)
    import dispatch as dispatch_mod
    return _dumps(dispatch_mod.dispatch(task_id, actor=auth.actor(principal)))


@mcp.tool()
def ingest_and_triage(kind: str, title: str, text: str, ctx: Context) -> str:
    """Ingest an artifact (email / transcript / document / note) into the RAG corpus AND triage it
    against the plan. Returns {summary, proposals, new_tasks, sources} — proposals are NOT applied
    (use update_task / create_task to apply). kind: email|transcript|document|note."""
    _require_write(ctx)
    return _dumps(intake_mod.ingest_and_triage(kind, title, text))


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
