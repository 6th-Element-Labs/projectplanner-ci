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
import re

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

store.init_project_registry()
for _pid in store.project_ids():  # ensure every project's schema exists (the web app normally seeds them)
    store.init_db(_pid)
    store.seed_if_empty(_pid)

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
        "Multi-project planning board. Every task/board tool takes a `project` arg. Built-ins are "
        "'maxwell' (default — TEEP Barnett Phase-1 pilot), 'helm' (the Helm marine-chartplotter "
        "build), and 'switchboard' (the live dogfood board for the agent coordination layer); "
        "additional boards may be created with create_project and discovered with list_projects. "
        "ALWAYS pass project='helm' to read or update Helm tasks (workstreams ENGINE/CHART/CONTRACT/"
        "OWNSHIP/ROUTE/AIS/ALARM/WX/...). ALWAYS pass project='switchboard' for Switchboard/"
        "projectplanner product work. Omit project (or use 'maxwell') only for the Maxwell plan. "
        "Writes go ONLY to the named board — they can never cross. At boot, call prepare_agent_session "
        "with your runtime plus assigned task_id/lane/project; it lists boards, validates the selected "
        "project, and returns a project-bound startup prompt. Use search_tasks/get_task to read, board_summary "
        "for the at-a-glance board, get_plan_signals for health, and create_task/update_task/add_comment "
        "to change a plan. ask_plan also takes project (Helm answers are board-grounded incl. "
        "code-audit comments); doc_search remains Maxwell-only.\n\n"
        "SESSION-START HANDSHAKE: (0) call prepare_agent_session(...) if you were assigned a task, "
        "lane, or project and follow its selected project; (1) call get_working_agreement(project) "
        "and follow its rules for the whole session; (2) register_agent; (3) drain your inbox. "
        "DEFINITION OF DONE: you may move a task only as far as 'In Review' (via complete with "
        "branch+SHA evidence) — you MUST NOT set status to 'Done'. Only the merge webhook marks "
        "Done. Push your branch before you claim progress; we squash-merge, so trust the board's "
        "merged_sha, never `git branch --merged`. See ADR-0003/0004."
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


def _resolve_project_input(project: str) -> str:
    """Accept either a project id or its display label, case-insensitively."""
    value = (project or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    for p in store.projects():
        if lowered in (p["id"].lower(), (p.get("label") or "").lower()):
            return p["id"]
    return lowered


def _project_ids_for_task(task_id: str) -> list[str]:
    tid = (task_id or "").strip().upper()
    if not tid:
        return []
    matches = []
    for pid in store.project_ids():
        try:
            if store.get_task(tid, project=pid):
                matches.append(pid)
        except Exception:
            continue
    return matches


def _project_ids_for_lane(lane: str) -> list[str]:
    ws = (lane or "").strip().upper()
    if not ws:
        return []
    matches = []
    for pid in store.project_ids():
        try:
            if store.list_tasks(workstream=ws, project=pid):
                matches.append(pid)
        except Exception:
            continue
    return matches


def _task_boot_brief(task):
    if not task:
        return None
    return {
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "workstream": task.get("_wsId"),
        "workstream_name": task.get("_wsName"),
        "owner_person_or_role": task.get("owner_person_or_role"),
        "depends_on": task.get("depends_on") or [],
    }


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:48].strip("-") or "session"


def _suggest_agent_id(runtime: str, agent_id: str, task_id: str, lane: str, task) -> str:
    if (agent_id or "").strip():
        return agent_id.strip()
    rt = (runtime or "").strip() or "<runtime>"
    if task_id:
        title = (task or {}).get("title") if task else ""
        return f"{rt}/{task_id}-{_slugify(title or lane or 'work')}"
    if lane:
        return f"{rt}/{lane}-{_slugify('work')}"
    return f"{rt}/<TASK-ID>-<slug>"


def _project_label(project: str) -> str:
    for p in store.projects():
        if p["id"] == project:
            return p.get("label") or project
    return project


def _agent_bootstrap_prompt(project: str, agent_id: str, task_id: str, lane: str) -> str:
    lines = [
        f'You are enlisting on Switchboard project="{project}" ({_project_label(project)}).',
        f'Every board/MCP call in this session must include project="{project}".',
        'Do not use project="helm", project="maxwell", or any other board unless prepare_agent_session selects it.',
        "Boot sequence:",
        f'1. get_working_agreement(project="{project}")',
        f'2. register_agent(agent_id="{agent_id}", runtime="<your-runtime>", lane="{lane or "<lane>"}", '
        f'task_id="{task_id or "<task-id>"}", project="{project}", control_json="{{...}}", protocol_json="{{...}}")',
        f'3. list_unacked_messages(to_agent="{agent_id}", project="{project}")',
        f'4. list_unblock_requests(owner_agent="{agent_id}", project="{project}")',
    ]
    if task_id:
        lines.append(f'5. get_task(task_id="{task_id}", project="{project}")')
    elif lane:
        lines.append(f'5. search_tasks(workstream="{lane}", project="{project}")')
    else:
        lines.append(f'5. board_summary(project="{project}")')
    lines.append('If a task or lane is missing, stop and call prepare_agent_session again before doing work.')
    return "\n".join(lines)


def _first_calls(project: str, agent_id: str, runtime: str, model: str,
                 task_id: str, lane: str, agreement: dict) -> list[dict]:
    register_args = {
        "agent_id": agent_id,
        "runtime": runtime or "<runtime>",
        "model": model or "",
        "lane": lane or "",
        "task_id": task_id or "",
        "control_json": json.dumps({"mode": "advisory_poll"}, sort_keys=True),
        "protocol_json": json.dumps(agreement.get("protocol") or {}, sort_keys=True),
        "project": project,
    }
    calls = [
        {"tool": "get_working_agreement", "args": {"project": project}},
        {"tool": "register_agent", "args": register_args},
        {"tool": "list_unacked_messages", "args": {"to_agent": agent_id, "project": project}},
        {"tool": "list_unblock_requests", "args": {"owner_agent": agent_id, "project": project}},
    ]
    if task_id:
        calls.append({"tool": "get_task", "args": {"task_id": task_id, "project": project}})
    elif lane:
        calls.append({"tool": "search_tasks", "args": {"workstream": lane, "project": project}})
    else:
        calls.append({"tool": "board_summary", "args": {"project": project}})
    return calls


# ---- read tools (open) ---------------------------------------------------
@mcp.tool()
def list_projects() -> str:
    """List all routable project boards. Returns [{id,label,pretitle}] plus the default id."""
    return _dumps({"projects": store.projects(), "default": store.DEFAULT_PROJECT})


@mcp.tool()
def prepare_agent_session(runtime: str = "", agent_id: str = "", project: str = "",
                          task_id: str = "", lane: str = "", model: str = "",
                          intent: str = "") -> str:
    """Boot-time resolver for autonomous agents.

    Call this BEFORE get_working_agreement/register_agent/claim_next. It lists available
    project boards, resolves task_id or lane to the correct project when possible, validates
    any explicit project choice, and returns a project-bound startup prompt plus exact first
    MCP calls. This prevents agents from silently landing on the default Maxwell board or
    doing Vulkan work on Helm.
    """
    tid = (task_id or "").strip().upper()
    ws = (lane or "").strip().upper()
    selected = _resolve_project_input(project)
    task_matches = _project_ids_for_task(tid)
    lane_matches = _project_ids_for_lane(ws)
    warnings: list[str] = []
    projects_payload = store.projects()

    if selected and not store.has_project(selected):
        return _dumps({
            "ok": False,
            "status": "unknown_project",
            "error": f"project '{project}' is not a routable Switchboard project",
            "projects": projects_payload,
            "selected_project": None,
            "task_matches": task_matches,
            "lane_matches": lane_matches,
            "next_step": "Pick one of projects[].id and call prepare_agent_session again.",
        })

    if selected and tid and selected not in task_matches:
        return _dumps({
            "ok": False,
            "status": "project_task_mismatch" if task_matches else "task_not_found",
            "error": (
                f"task_id '{tid}' is not on project '{selected}'"
                + (f"; it exists on {', '.join(task_matches)}" if task_matches else "")
            ),
            "projects": projects_payload,
            "selected_project": selected,
            "task_matches": task_matches,
            "lane_matches": lane_matches,
            "next_step": (
                f"Use project='{task_matches[0]}' for task_id='{tid}'."
                if len(task_matches) == 1 else
                "Choose the intended project explicitly, or create the missing task on that project."
            ),
        })

    if selected and ws and lane_matches and selected not in lane_matches:
        return _dumps({
            "ok": False,
            "status": "project_lane_mismatch",
            "error": f"lane '{ws}' is not on project '{selected}'; it exists on {', '.join(lane_matches)}",
            "projects": projects_payload,
            "selected_project": selected,
            "task_matches": task_matches,
            "lane_matches": lane_matches,
            "next_step": f"Use project='{lane_matches[0]}' for lane='{ws}'." if len(lane_matches) == 1
            else "Choose the intended project explicitly.",
        })

    if not selected:
        candidate_sets = [set(x) for x in (task_matches, lane_matches) if x]
        if candidate_sets:
            common = set.intersection(*candidate_sets)
            candidates = sorted(common or set.union(*candidate_sets))
            if len(candidates) == 1:
                selected = candidates[0]
                warnings.append(f"project inferred from {'task_id' if tid else 'lane'}")
            else:
                return _dumps({
                    "ok": False,
                    "status": "choice_required",
                    "error": "task/lane matches multiple projects" if candidates else "no project could be inferred",
                    "projects": projects_payload,
                    "selected_project": None,
                    "task_matches": task_matches,
                    "lane_matches": lane_matches,
                    "next_step": "Call prepare_agent_session again with project set to one of projects[].id.",
                })
        else:
            return _dumps({
                "ok": False,
                "status": "choice_required",
                "error": "no project, task_id, or lane selected",
                "projects": projects_payload,
                "selected_project": None,
                "task_matches": task_matches,
                "lane_matches": lane_matches,
                "next_step": "Choose a project id from projects[] before register_agent or claim_next.",
            })

    task = store.get_task(tid, project=selected) if tid else None
    if task and ws and task.get("_wsId") != ws:
        return _dumps({
            "ok": False,
            "status": "task_lane_mismatch",
            "error": f"task_id '{tid}' belongs to lane '{task.get('_wsId')}', not lane '{ws}'",
            "projects": projects_payload,
            "selected_project": selected,
            "task": _task_boot_brief(task),
            "next_step": f"Use lane='{task.get('_wsId')}' or pick the correct task.",
        })
    if task and not ws:
        ws = task.get("_wsId") or ""

    agreement = store.get_working_agreement(project=selected)
    chosen_agent_id = _suggest_agent_id(runtime, agent_id, tid, ws, task)
    return _dumps({
        "ok": True,
        "status": "ready",
        "projects": projects_payload,
        "selected_project": selected,
        "selected_project_label": _project_label(selected),
        "task_matches": task_matches,
        "lane_matches": lane_matches,
        "task": _task_boot_brief(task),
        "lane": ws,
        "agent_id": chosen_agent_id,
        "intent": intent,
        "warnings": warnings,
        "working_agreement": agreement,
        "first_calls": _first_calls(selected, chosen_agent_id, runtime, model, tid, ws, agreement),
        "startup_prompt": _agent_bootstrap_prompt(selected, chosen_agent_id, tid, ws),
    })


@mcp.tool()
def search_tasks(workstream: str = "", status: str = "", owner_person: str = "",
                 blocking: bool = False, query: str = "", project: str = "maxwell") -> str:
    """Filter a plan's tasks. project selects the board ('maxwell' default, 'helm', or
    'switchboard'). All other args optional: workstream id (SSO/SEN/... for Maxwell;
    ENGINE/CHART/... for Helm; PROTO/ADAPTER/ENFORCE/... for Switchboard), status
    (Not Started|In Progress|In Review|Blocked|Done), owner_person substring, blocking, free-text query.
    Returns a JSON list of {task_id,title,status,owner_person_or_role,workstream,...}."""
    return _dumps(agent._search_tasks({
        "workstream": workstream, "status": status, "owner_person": owner_person,
        "blocking": blocking, "query": query}, project=project))


@mcp.tool()
def get_task(task_id: str, project: str = "maxwell") -> str:
    """Full detail of one task: description, all fields, dependencies, and recent activity.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    t = store.get_task(task_id, project=project)
    return _dumps(agent._task_brief(t, full=True)) if t else "no such task"


@mcp.tool()
def board_summary(project: str = "maxwell") -> str:
    """Full board snapshot: project name + rollups, then one line per task.
    Use ONCE at session start for orientation. For recurring 'has anything changed?' checks
    use get_lane_delta instead — it returns only what changed and costs ~50 tokens when nothing
    did vs ~3000-5000 tokens here. project selects the board ('maxwell' default, 'helm',
    or 'switchboard')."""
    return (f"Project: {store.get_meta('project', project=project)}\n"
            f"Rollups: {_dumps(store.get_meta('rollups', project=project) or {})}\n\n"
            f"{agent.board_summary_text(project=project)}")


@mcp.tool()
def get_lane_delta(project: str = "maxwell", lane: str = "", since_cursor: int = 0) -> str:
    """Efficient poll replacement — returns ONLY tasks that changed since your last call.
    Use this instead of board_summary in any polling loop. Costs ~50 tokens when nothing
    changed (empty updates list) vs 3000-5000 tokens for a full board_summary.

    project: 'maxwell', 'helm', or 'switchboard'. lane: workstream id to filter (e.g. 'ENGINE',
    'CHART', 'OWNSHIP', 'ADAPTER') — leave blank for all workstreams. since_cursor: the cursor value from your
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
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    return _dumps(signals.compute_plan_signals(project=project))


@mcp.tool()
def get_working_agreement(project: str = "maxwell") -> str:
    """Connect-time policy for agents: definition of done, branch convention, merge strategy,
    canonical main SHA, and the session-start sequence. Call before register_agent."""
    return _dumps(store.get_working_agreement(project=project))


@mcp.tool()
def ask_plan(question: str, project: str = "maxwell") -> str:
    """Ask the plan-wide agent a question about a board. project selects it ('maxwell' default,
    'helm', or 'switchboard'). Helm and Switchboard answers are grounded in the live board; Maxwell
    also grounds in the plan docs via RAG. Returns a reasoned answer (+ sources) and, when relevant,
    a proposed task change (NOT applied — call update_task to apply it)."""
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
    not corrupt state. project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.release_files(lease_id, project=project))


@mcp.tool()
def check_files(files: str, project: str = "maxwell") -> str:
    """Check whether any of the given file paths are held by an active lease.
    files: comma or newline-separated list of paths.
    Returns a list of {file, held_by, task_id, expires_at} for files that ARE held.
    Empty list means all files are free — safe to edit without claiming first (though
    calling claim_files is strongly preferred to avoid races).
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    file_list = [f.strip() for f in files.replace("\n", ",").split(",") if f.strip()]
    if not file_list:
        return _dumps([])
    return _dumps(store.check_files(file_list, project=project))


@mcp.tool()
def list_active_leases(project: str = "maxwell") -> str:
    """All active file leases on the board — who holds what, and when it expires.
    Use to see which agents are currently active and which files they have claimed.
    Expired and released leases are not shown.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    return _dumps(store.list_active_leases(project=project))


# ---- IXP-core runtime lifecycle -----------------------------------------
@mcp.tool()
def register_agent(agent_id: str, runtime: str, ctx: Context, model: str = "",
                   lane: str = "", task_id: str = "", ttl_s: int = 120,
                   control_json: str = "{}", protocol_json: str = "{}",
                   project: str = "maxwell") -> str:
    """Register a live agent session. Call at session start before claiming work.
    control_json advertises truthful control fidelity, e.g. {"mode":"advisory_poll"}.
    protocol_json advertises the adapter protocol envelope returned by get_working_agreement."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        control = json.loads(control_json or "{}")
    except Exception:
        return _dumps({"error": "control_json must be a JSON object string"})
    try:
        protocol = json.loads(protocol_json or "{}")
    except Exception:
        return _dumps({"error": "protocol_json must be a JSON object string"})
    return _dumps(store.register_agent(
        agent_id=agent_id, runtime=runtime, model=model, lane=lane, task_id=task_id,
        ttl_s=ttl_s, control=control, protocol=protocol, principal_id=principal["id"],
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
def register_host(host_id: str, runtimes_json: str, ctx: Context,
                  hostname: str = "", repo_root: str = "",
                  agent_host_version: str = "0.1.0",
                  limits_json: str = "{}", heartbeat_ttl_s: int = 60,
                  project: str = "maxwell") -> str:
    """Register an always-on Agent Host that can wake/start runtimes.

    runtimes_json is a JSON list, e.g. [{"runtime":"claude-code","lanes":["ADAPTER"],
    "capabilities":["python","docs"]}]. limits_json can include {"max_sessions":2}.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        runtimes = json.loads(runtimes_json or "[]")
        limits = json.loads(limits_json or "{}")
    except Exception:
        return _dumps({"error": "runtimes_json and limits_json must be valid JSON"})
    return _dumps(store.register_host(
        {"host_id": host_id, "hostname": hostname, "repo_root": repo_root,
         "agent_host_version": agent_host_version, "runtimes": runtimes,
         "limits": limits, "heartbeat_ttl_s": heartbeat_ttl_s},
        principal_id=principal["id"], actor=auth.actor(principal), project=project))


@mcp.tool()
def heartbeat_host(host_id: str, ctx: Context, active_sessions: int = -1,
                   capacity_json: str = "{}", status: str = "online",
                   last_error: str = "", project: str = "maxwell") -> str:
    """Renew liveness/capacity for an Agent Host."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        capacity = json.loads(capacity_json or "{}")
    except Exception:
        return _dumps({"error": "capacity_json must be a JSON object string"})
    return _dumps(store.heartbeat_host(
        host_id, active_sessions=(None if active_sessions < 0 else active_sessions),
        capacity=capacity, status=status, last_error=last_error,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def list_agent_hosts(project: str = "maxwell", runtime: str = "", lane: str = "",
                     capability: str = "", include_stale: bool = False) -> str:
    """List registered Agent Hosts and their wake capacity."""
    return _dumps(store.list_agent_hosts(runtime=runtime, lane=lane,
                                        capability=capability,
                                        include_stale=include_stale,
                                        project=project))


@mcp.tool()
def host_status(host_id: str, project: str = "maxwell") -> str:
    """Return one Agent Host's inventory, liveness, capacity, and wake counts."""
    return _dumps(store.host_status(host_id, project=project))


@mcp.tool()
def request_wake(selector_json: str, reason: str, ctx: Context,
                 source: str = "", policy_json: str = "{}", task_id: str = "",
                 idem_key: str = "", project: str = "maxwell") -> str:
    """Create a durable wake intent for an absent runtime/session.

    selector_json includes runtime/agent_id/lane/capabilities. Example:
    {"runtime":"claude-code","agent_id":"claude-code","lane":"ADAPTER"}.
    """
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        selector = json.loads(selector_json or "{}")
        policy = json.loads(policy_json or "{}")
    except Exception:
        return _dumps({"error": "selector_json and policy_json must be valid JSON"})
    return _dumps(store.request_wake(
        selector=selector, reason=reason, source=source or auth.actor(principal),
        policy=policy, task_id=task_id or None, principal_id=principal["id"],
        actor=auth.actor(principal), idem_key=idem_key, project=project))


@mcp.tool()
def list_wake_intents(project: str = "maxwell", status: str = "", host_id: str = "",
                      runtime: str = "") -> str:
    """List durable wake intents. status can be pending|claimed|completed|failed|cancelled."""
    return _dumps(store.list_wake_intents(status=status, host_id=host_id,
                                         runtime=runtime, project=project))


@mcp.tool()
def claim_wake(host_id: str, wake_id: str, ctx: Context,
               project: str = "maxwell") -> str:
    """Atomically assign one pending wake intent to an eligible Agent Host."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.claim_wake(host_id, wake_id, actor=auth.actor(principal),
                                  project=project))


@mcp.tool()
def complete_wake(wake_id: str, ctx: Context, runner_session_id: str = "",
                  agent_id: str = "", result_json: str = "{}",
                  project: str = "maxwell") -> str:
    """Record wake success/failure after the host daemon starts or fails to start a runtime."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        result = json.loads(result_json or "{}")
    except Exception:
        return _dumps({"error": "result_json must be a JSON object string"})
    return _dumps(store.complete_wake(
        wake_id, runner_session_id=runner_session_id, agent_id=agent_id,
        result=result, actor=auth.actor(principal), project=project))


@mcp.tool()
def cancel_wake(wake_id: str, ctx: Context, reason: str = "cancelled",
                project: str = "maxwell") -> str:
    """Cancel a pending or claimed wake intent."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.cancel_wake(wake_id, reason=reason,
                                   actor=auth.actor(principal), project=project))


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
                 outcome_id: str = "",
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
        claim_id=claim_id or None, outcome_id=outcome_id or None,
        agent_id=agent_id or None,
        principal_id=principal["id"], runtime=runtime, call_site=call_site,
        provider=provider, model=model, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens or None, cost_usd=cost_usd,
        metadata=metadata, request_id=request_id or None, project=project))


@mcp.tool()
def record_outcome(ctx: Context, outcome_type: str, title: str,
                   task_id: str = "", claim_id: str = "", epic_id: str = "",
                   status: str = "proposed", verifier: str = "",
                   verification: str = "", evidence_json: str = "{}",
                   value_json: str = "{}", project: str = "maxwell") -> str:
    """Record an OXP outcome. Proposed outcomes are pending value; only verified outcomes
    count in cost-per-outcome denominators."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
        value = json.loads(value_json or "{}")
    except Exception:
        return _dumps({"error": "evidence_json and value_json must be JSON object strings"})
    return _dumps(store.record_outcome(
        outcome_type=outcome_type, title=title, task_id=task_id or None,
        claim_id=claim_id or None, epic_id=epic_id or None, status=status,
        verifier=verifier, verification=verification, evidence=evidence,
        value=value, actor=auth.actor(principal), project=project))


@mcp.tool()
def verify_outcome(outcome_id: str, ctx: Context, verifier: str = "",
                   verification: str = "", evidence_json: str = "{}",
                   project: str = "maxwell") -> str:
    """Mark an outcome verified so it enters Tally's denominator."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
    except Exception:
        return _dumps({"error": "evidence_json must be a JSON object string"})
    return _dumps(store.verify_outcome(
        outcome_id, verifier=verifier or auth.actor(principal),
        verification=verification, evidence=evidence,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def reject_outcome(outcome_id: str, reason: str, ctx: Context,
                   verifier: str = "", project: str = "maxwell") -> str:
    """Reject a proposed outcome. Rejected outcomes remain auditable but never count."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.reject_outcome(
        outcome_id, verifier=verifier or auth.actor(principal), reason=reason,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def create_kpi(ctx: Context, name: str, unit: str, direction: str,
               owner: str = "", baseline_value: float = 0.0,
               current_value: float = 0.0, target_value: float = 0.0,
               period: str = "", project: str = "maxwell") -> str:
    """Create a KPI that outcomes can move. direction: increase|decrease|maintain."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.create_kpi(
        name=name, unit=unit, direction=direction, owner=owner,
        baseline_value=baseline_value if baseline_value else None,
        current_value=current_value if current_value else None,
        target_value=target_value if target_value else None,
        period=period, actor=auth.actor(principal), project=project))


@mcp.tool()
def update_kpi_value(kpi_id: str, current_value: float, ctx: Context,
                     evidence_json: str = "{}", project: str = "maxwell") -> str:
    """Update the current value for a KPI."""
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        evidence = json.loads(evidence_json or "{}")
    except Exception:
        return _dumps({"error": "evidence_json must be a JSON object string"})
    return _dumps(store.update_kpi_value(
        kpi_id, current_value=current_value, evidence=evidence,
        actor=auth.actor(principal), project=project))


@mcp.tool()
def link_outcome_to_kpi(ctx: Context, outcome_id: str, kpi_id: str,
                        contribution: float = 0.0, contribution_unit: str = "",
                        confidence: str = "directional", rationale: str = "",
                        project: str = "maxwell") -> str:
    """Link a verified or proposed outcome to a KPI with measured|estimated|directional
    confidence. Only verified outcome links count in cost-per-KPI movement."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.link_outcome_to_kpi(
        outcome_id=outcome_id, kpi_id=kpi_id,
        contribution=contribution if contribution else None,
        contribution_unit=contribution_unit, confidence=confidence,
        rationale=rationale, actor=auth.actor(principal), project=project))


@mcp.tool()
def get_task_tally(task_id: str, project: str = "maxwell") -> str:
    """Tally rollup for one task: spend by source, total tokens/cost, and outcome denominator."""
    return _dumps(store.task_tally(task_id, project=project))


@mcp.tool()
def get_kpi_tally(kpi_id: str, project: str = "maxwell") -> str:
    """KPI rollup: linked outcomes, spend, verified contribution, and cost per movement unit."""
    return _dumps(store.kpi_tally(kpi_id, project=project))


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
                       ack_timeout_seconds: float = 0,
                       on_ack_timeout: str = "notify_sender",
                       signal: str = "", priority: int = 0,
                       idem_key: str = "") -> str:
    """Send a directed message to another agent session. Unlike add_comment (bulletin
    board, fire-and-forget), this has an ack/read-receipt so the sender can confirm
    the message landed before acting on the assumption it was received.

    from_agent / to_agent: stable agent-session identifiers (e.g. 'claude/ENGINE-11').
    task_id: the task this message is about (optional).
    requires_ack: if true, the receiving agent should call ack_message to confirm receipt.
    ack_deadline_minutes: how long the sender will wait for an ack (0 = no deadline).
    ack_timeout_seconds: equivalent seconds-based alias; used when minutes is 0.

    Returns the message record including its id. Pass the id to get_message_status to
    check whether the recipient has acked."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.send_agent_message(
        from_agent, to_agent, message,
        task_id=task_id or None,
        requires_ack=requires_ack,
        ack_deadline_minutes=ack_deadline_minutes or None,
        ack_timeout_seconds=ack_timeout_seconds or None,
        on_ack_timeout=on_ack_timeout,
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
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.ack_message(message_id, response=response,
                                    actor=auth.actor(principal), project=project))


@mcp.tool()
def list_unacked_messages(to_agent: str, project: str = "maxwell") -> str:
    """Your incoming message inbox — messages directed to you that have not been acked.
    Call at session start and after completing a task to check for coordination messages
    from other agents. to_agent: your agent-session id (e.g. 'claude/CHART-8').
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    return _dumps(store.list_unacked_messages(to_agent, project=project))


@mcp.tool()
def get_message_status(message_id: int, project: str = "maxwell") -> str:
    """Check whether a message you sent has been acked. Returns the full message record
    including acked_at and ack_response if the recipient has responded.
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
    r = store.get_message_status(message_id, project=project)
    return _dumps(r) if r else "message not found"


@mcp.tool()
def list_pending_acks(project: str = "maxwell", agent_id: str = "") -> str:
    """List unacked requires_ack messages plus durable monitor state. agent_id filters to
    messages either sent by or addressed to that agent. This is the protocol-native way to see
    what is still waiting on another agent."""
    return _dumps(store.list_pending_acks(agent_id=agent_id, project=project))


@mcp.tool()
def list_monitors(project: str = "maxwell", status: str = "", kind: str = "") -> str:
    """List durable Switchboard monitors. status can be pending|fired|resolved|cancelled;
    kind can be ack_deadline."""
    return _dumps(store.list_coordination_monitors(status=status, kind=kind, project=project))


@mcp.tool()
def sweep_monitors(ctx: Context, project: str = "maxwell") -> str:
    """Evaluate durable monitors now: resolve acked messages and fire timed-out ack monitors.
    This is also what the Switchboard-owned systemd timer calls."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.sweep_coordination_monitors(project=project))


@mcp.tool()
def resolve_monitor(monitor_id: str, ctx: Context, project: str = "maxwell",
                    reason: str = "manual") -> str:
    """Manually resolve a durable monitor after an operator handles it outside the normal path."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.resolve_monitor(monitor_id, reason=reason,
                                       actor=auth.actor(principal), project=project))


@mcp.tool()
def cancel_monitor(monitor_id: str, ctx: Context, project: str = "maxwell",
                   reason: str = "cancelled") -> str:
    """Cancel a durable monitor that should no longer fire."""
    principal = _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.cancel_monitor(monitor_id, reason=reason,
                                      actor=auth.actor(principal), project=project))


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
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
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
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
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
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
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
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
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
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
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
    choosing an approach. project: 'maxwell' (default), 'helm', or 'switchboard'."""
    return _dumps(store.list_decisions(task_id=task_id or None,
                                      status=status, project=project))


@mcp.tool()
def get_decision(decision_id: int, project: str = "maxwell") -> str:
    """Fetch a single decision record by id. Use when list_decisions refers to a
    decision you want to read in full (context + rationale).
    project: 'maxwell' (default), 'helm', or 'switchboard'."""
    r = store.get_decision(decision_id, project=project)
    return _dumps(r) if r else "decision not found"


# ---- task write tools (Switchboard bearer-principal authenticated) -------
@mcp.tool()
def create_project(name: str, ctx: Context, project_id: str = "", label: str = "",
                   pretitle: str = "") -> str:
    """Create a new isolated project board and make it routable by all board tools.

    Authenticates against project='switchboard' with write:system. `name` is the human
    name; `project_id` is optional and defaults to a lowercase slug, e.g. name='Vulkan'
    creates project='vulkan'. Returns the created/existing project record.
    """
    principal = _require_write(ctx, "switchboard", ("write:system",))
    result = store.create_project(name=name, project_id=project_id, label=label,
                                  pretitle=pretitle, actor=auth.actor(principal))
    return _dumps(result)


@mcp.tool()
def update_task(task_id: str, ctx: Context, title: str = "", description: str = "", status: str = "",
                owner_org: str = "", owner_person_or_role: str = "", assignee: str = "",
                phase: str = "", start_date: str = "", finish_date: str = "",
                risk_level: str = "", is_blocking: str = "", depends_on: str = "",
                project: str = "maxwell") -> str:
    """Update only the fields you pass on a task. status: Not Started|In Progress|In Review|Blocked|Done;
    dates: YYYY-MM-DD; is_blocking: 'true'/'false'. depends_on: comma/space-separated task ids that
    REPLACE this task's dependency list (e.g. 'TOOLS-7, SHELL-1'); pass 'none' to clear it (for an
    incremental edge use add_dependency/remove_dependency). Audited as the authenticated actor.
    project selects the board ('maxwell' default, 'helm', or 'switchboard') — writes go ONLY to that board."""
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
    """Create a task in a workstream (SSO/SEN/... for Maxwell; ENGINE/CHART/... for Helm;
    PROTO/ADAPTER/ENFORCE/... for Switchboard). depends_on:
    comma/space-separated task ids this task dependsOn (e.g. 'BOAT-1, WX-10'). Returns the created task.
    Actor 'MCP'. project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
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
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
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
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
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
    project selects the board ('maxwell' default, 'helm', or 'switchboard')."""
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
