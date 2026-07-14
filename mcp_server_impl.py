#!/usr/bin/env python3
"""MCP server for the Project Maxwell plan (Phase 1.5 — see docs/AGENT_ROADMAP.md).

A second front door over the SAME primitives the web agent uses: read tasks/docs,
ask the plan agent, and create/update tasks — from Cursor, Claude Desktop, Claude
Code, etc. Runs as its own process (Streamable HTTP on 127.0.0.1:8111); Caddy routes
https://plan.taikunai.com/mcp here. Reuses store/rag/agent in-process and shares the
SQLite file (WAL) with the web app.

Auth: reads and writes require bearer when PM_AUTH_MODE=required (MCPAuthMiddleware).
`dev-open` passes through for local/hermetic runs. PM_MCP_TOKEN and explicit store
principals remain supported.
"""
import json
import os

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import auth
import digest as digest_mod
import intake as intake_mod
import notify as notify_mod
import project_contract as project_contract_service
import rag
import store
import scripts.switchboard_path  # noqa: F401
from mcp_observability import MCPObservability
from mcp_dispatch import MCPToolDispatcher
from mcp_http_timing import MCPServerTimingMiddleware
from mcp_observability_http import MCPObservabilityEndpoint
from mcp_auth import MCPAuthMiddleware
from switchboard.mcp.tools import board as board_tools
from switchboard.mcp.tools import boot as boot_tools
from switchboard.mcp.tools import decisions as decision_tools
from switchboard.mcp.tools import projects as project_tools
from switchboard.mcp.tools import provider_credentials as provider_credential_tools
from switchboard.mcp.tools import reviews as review_tools
from switchboard.mcp.tools import tasks as task_tools
from switchboard.mcp.tools import claims as claim_tools  # noqa: E402
from switchboard.mcp.tools import wakes as wake_tools  # noqa: E402
from switchboard.mcp.tools import agents as agent_tools  # noqa: E402
from switchboard.mcp.tools import messaging as messaging_tools  # noqa: E402
from switchboard.mcp.tools import leases as lease_tools  # noqa: E402
from switchboard.mcp.tools import work_sessions as work_session_tools  # noqa: E402
from switchboard.mcp.tools import resources as resource_tools  # noqa: E402
from switchboard.mcp.tools import tally as tally_tools  # noqa: E402
from switchboard.mcp.tools import runner as runner_tools  # noqa: E402
from switchboard.mcp.tools import external_effects as external_effects_tools  # noqa: E402
from switchboard.mcp.tools import deliverables as deliverable_tools  # noqa: E402

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
        "project, and returns a project-bound startup prompt plus a project-level project_contract. "
        "For lane ownership, deliverables, dependencies, and file-boundary hints, use "
        "get_project_contract/project_contract rather than assuming repo-local docs are universal. "
        "Use search_tasks/get_task to read, board_summary for the at-a-glance board, get_plan_signals "
        "for health, and create_task/update_task/add_comment to change a plan. ask_plan and "
        "doc_search also take project — each board has its own segmented corpus.\n\n"
        "SESSION-START HANDSHAKE: (0) call prepare_agent_session(...) if you were assigned a task, "
        "lane, or project and follow its selected project; (1) call get_working_agreement(project) "
        "and follow its rules for the whole session; (2) register_agent; (3) drain your inbox. "
        "DEFINITION OF DONE: follow get_working_agreement(project). Agents use "
        "complete_claim(evidence=...) to record branch/head_sha/PR/offline evidence, release the "
        "claim, and move work to In Review. Agents do not mark Done. Done is reserved for "
        "GitHub/default-branch provenance recorded by webhook/reconcile, or verifier-stamped "
        "offline_evidence for non-PR work. Push your branch before you claim progress; we "
        "squash-merge, so trust the board's recorded merged_sha over local branch state. SAFE "
        "MERGE: if you are authorized to merge, fetch origin, rebase/merge onto the intended "
        "target branch, resolve conflicts intentionally, rerun relevant tests, push the updated "
        "branch, merge through GitHub/merge queue only when checks/review are green, then fetch "
        "the target branch and record the resulting merged_sha; never set Done manually."
    ),
    host="127.0.0.1",
    port=_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=_SECURITY,
)

# FastMCP registers functions at decoration time.  Replacing the instance's
# decorator here instruments every tool below without duplicating timing code in
# ~150 handlers, while functools.wraps preserves the schemas FastMCP derives.
_mcp_observability = MCPObservability()
# HARDEN-63: attribute store lock-wait retries to the in-flight tool so per-tool
# contention is visible even when the retry loop transparently recovers.
try:
    store.register_lock_wait_observer(_mcp_observability.note_sqlite_lock_wait)
except Exception:
    pass
_mcp_dispatch = MCPToolDispatcher(
    # These are deliberately tiny diagnostics. Keeping them inline proves that
    # the event loop remains responsive while ordinary sync tools run in workers.
    inline_tools={"control_plane_probe", "get_mcp_observability"},
)
_register_mcp_tool = mcp.tool


def _observed_mcp_tool(*args, **kwargs):
    register = _register_mcp_tool(*args, **kwargs)

    def observed_register(fn):
        observed = _mcp_observability.wrap(fn)
        register(_mcp_dispatch.wrap(observed))
        # Keep direct Python callers and the existing hermetic tests synchronous;
        # only FastMCP's registered request handler needs worker dispatch.
        return fn

    return observed_register


mcp.tool = _observed_mcp_tool


def _dumps(obj) -> str:
    """json.dumps with sort_keys=True — deterministic serialization for prompt-cache hits.
    Stable key order means identical responses share a cache hit across agent sessions."""
    return json.dumps(obj, sort_keys=True)


def _require_write(ctx, project: str = "maxwell", scopes=("write:tasks",)):
    """Gate writes through the shared Switchboard bearer-principal path."""
    try:
        principal = auth.authenticate(project, auth.bearer_from_mcp_context(ctx),
                                      scopes, dev_actor="MCP")
    except PermissionError as e:
        raise ValueError(str(e))
    # HARDEN-63: this call took the write path — feed the write-latency histogram.
    _mcp_observability.mark_write()
    return principal


def _require_read(ctx, project: str = "maxwell", scopes=("read",)):
    """Gate sensitive reads through the selected project's bearer scopes."""
    try:
        return auth.authenticate(project, auth.bearer_from_mcp_context(ctx),
                                 scopes, dev_actor="MCP")
    except PermissionError as e:
        raise ValueError(str(e))


def _resolve_write_actor(principal, project: str = "maxwell", task_id: str = "",
                         agent_id: str = "", system_actor: str = "",
                         system_reason: str = ""):
    binding = store.resolve_write_actor(
        auth.actor(principal),
        project=project,
        task_id=task_id,
        agent_id=agent_id,
        system_actor=system_actor,
        system_reason=system_reason,
        principal_id=principal.get("id") or "",
    )
    if not binding.get("ok"):
        return binding
    return binding


def _write_binding_comment(task_id: str, binding, project: str = "maxwell") -> None:
    if not task_id or not isinstance(binding, dict):
        return
    if binding.get("binding") in ("principal", None):
        return
    store.append_activity(
        "principal.write_bound",
        "switchboard/identity",
        store.write_binding_activity_payload(binding),
        task_id=task_id,
        project=project,
    )


_task_tool_functions = task_tools.register_task_tools(
    mcp,
    task_tools.TaskToolServices(
        dumps=_dumps,
        require_write=_require_write,
        resolve_write_actor=_resolve_write_actor,
        write_binding_comment=_write_binding_comment,
    ),
)
globals().update(_task_tool_functions)

_review_tool_functions = review_tools.register_review_tools(
    mcp,
    review_tools.ReviewToolServices(
        dumps=_dumps,
        require_write=_require_write,
        resolve_write_actor=_resolve_write_actor,
        write_binding_comment=_write_binding_comment,
    ),
)
globals().update(_review_tool_functions)

_claim_tool_functions = claim_tools.register_claim_tools(
    mcp,
    claim_tools.ClaimToolServices(
        dumps=_dumps,
        require_write=_require_write,
        resolve_write_actor=_resolve_write_actor,
        write_binding_comment=_write_binding_comment,
    ),
)
globals().update(_claim_tool_functions)

_wake_tool_functions = wake_tools.register_wake_tools(
    mcp,
    wake_tools.WakeToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_wake_tool_functions)

_agent_tool_functions = agent_tools.register_agent_tools(
    mcp,
    agent_tools.AgentToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_agent_tool_functions)

_messaging_tool_functions = messaging_tools.register_messaging_tools(
    mcp,
    messaging_tools.MessagingToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_messaging_tool_functions)

_lease_tool_functions = lease_tools.register_lease_tools(
    mcp,
    lease_tools.LeaseToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_lease_tool_functions)

_work_session_tool_functions = work_session_tools.register_work_session_tools(
    mcp,
    work_session_tools.WorkSessionToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_work_session_tool_functions)

_resource_tool_functions = resource_tools.register_resource_tools(
    mcp,
    resource_tools.ResourceToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_resource_tool_functions)

_tally_tool_functions = tally_tools.register_tally_tools(
    mcp,
    tally_tools.TallyToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_tally_tool_functions)

_runner_tool_functions = runner_tools.register_runner_tools(
    mcp,
    runner_tools.RunnerToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_runner_tool_functions)

_external_effects_tool_functions = external_effects_tools.register_external_effects_tools(
    mcp,
    external_effects_tools.ExternalEffectsToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_external_effects_tool_functions)

_deliverable_tool_functions = deliverable_tools.register_deliverable_tools(
    mcp,
    deliverable_tools.DeliverableToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_deliverable_tool_functions)

# Compatibility aliases for direct Python callers while the monolith is strangled.
_dep_ids = task_tools.dep_ids
_unknown_ids = task_tools.unknown_ids

_board_tool_functions = board_tools.register_board_tools(
    mcp,
    board_tools.BoardToolServices(dumps=_dumps),
)
globals().update(_board_tool_functions)

_decision_tool_functions = decision_tools.register_decision_tools(
    mcp,
    decision_tools.DecisionToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_decision_tool_functions)

_project_tool_functions = project_tools.register_project_tools(
    mcp,
    project_tools.ProjectToolServices(
        dumps=_dumps,
        require_read=_require_read,
        require_write=_require_write,
        principal_actor=auth.actor,
    ),
)
globals().update(_project_tool_functions)

_provider_credential_tool_functions = (
    provider_credential_tools.register_provider_credential_tools(
        mcp,
        provider_credential_tools.ProviderCredentialToolServices(
            dumps=_dumps,
            require_read=_require_read,
            require_write=_require_write,
            principal_actor=auth.actor,
        ),
    )
)
globals().update(_provider_credential_tool_functions)

_boot_tool_functions = boot_tools.register_boot_tools(
    mcp,
    boot_tools.BootToolServices(dumps=_dumps),
)
globals().update(_boot_tool_functions)

# ---- read tools (open) ---------------------------------------------------
@mcp.tool()
def control_plane_probe(project: str = "maxwell", lane: str = "",
                        include_heavy: bool = False) -> str:
    """Tiny latency probe for MCP clients. Compare your client wall time to server_elapsed_ms.
    A large gap means time is outside Switchboard's Python/SQLite path."""
    from switchboard.application.queries.control_plane_probe import execute
    probe = execute(project=project, lane=lane, include_heavy=include_heavy)
    probe["mcp_framing"] = {
        "stateless_http": True,
        "approx_tool_payload_bytes": len(_dumps(probe).encode("utf-8")),
    }
    return _dumps(probe)


@mcp.tool()
def get_mcp_observability(tool: str = "", slow_limit: int = 50) -> str:
    """Process-local MCP health: per-tool p50/p99/max latency, per-tool SQLite
    lock-wait counts, write-path latency p50/p99 (per tool and aggregate), failures,
    and a bounded slow-call log. No arguments, results, tokens, or other request
    content are retained. tool optionally filters by exact tool name; slow_limit is
    capped by PM_MCP_SLOW_LOG_LIMIT. The same snapshot is scrapeable over plain HTTP
    at GET /observability for operators/monitors that don't speak MCP."""
    return _dumps(_mcp_observability.snapshot(tool=tool, slow_limit=slow_limit))


@mcp.tool()
def get_saturation_signals(project: str = "switchboard") -> str:
    """Box saturation dashboard (PERF-7): PSI pressure, sqlite lock-waits, webhook inbox
    depth, HTTP/MCP SLO status, load-shed recommendation, and alert list."""
    import saturation_signals as sat

    def _mcp_obs():
        window_s = float(os.environ.get("PM_SQLITE_LOCK_WAIT_WINDOW_S", "60"))
        snap = _mcp_observability.snapshot()
        store_waits = store.sqlite_lock_wait_count()
        store_window = store.sqlite_lock_waits_in_window(window_s)
        snap["sqlite_lock_waits"] = max(int(snap.get("sqlite_lock_waits") or 0), store_waits)
        snap["sqlite_lock_waits_window"] = store_window
        snap["sqlite_lock_wait_window_s"] = window_s
        return snap

    return _dumps(sat.compute_saturation_signals(
        project=project,
        mcp_obs_provider=_mcp_obs,
        request_obs_provider=lambda: {"routes": {}, "dropped_webhook_deliveries": 0},
    ))


@mcp.tool()
def get_narration_health(project: str = "switchboard") -> str:
    """NARRATE-13: bounded narration queue + generation-receipt snapshot — attempt-state depth,
    oldest-pending age, success/failure/fallback rates, model-token-cost totals, and alert flags
    (queue age, failure rate, dead letters). Read-only; indexed aggregates only."""
    import narration_ops
    return _dumps(narration_ops.narration_health(project))


@mcp.tool()
def narrate_now(entity_type: str, entity_id: str, ctx: Context,
                project: str = "switchboard", reason: str = "") -> str:
    """NARRATE-13: force (re)generation of an entity's current narration revision now. Audited,
    deduped (re-queues the current revision, no new visible effect), and still budget-gated —
    it does not bypass the NARRATE-12 generation policy. entity_type is task or deliverable."""
    import narration_ops
    principal = _require_write(ctx, project, ("write:system",))
    return _dumps(narration_ops.narrate_now(project, entity_type, entity_id,
                                            actor=auth.actor(principal), reason=reason))


@mcp.tool()
def reactivate_narration(event_id: str, ctx: Context, project: str = "switchboard",
                         action: str = "retry", reason: str = "") -> str:
    """NARRATE-13: authorized retry / dead-letter recovery on one narration request (audited).
    action='retry' returns a dead-lettered/errored request to the queue; action='dead_letter'
    parks a poison request. Operates on the existing row; immutable event fields are untouched."""
    import narration_ops
    principal = _require_write(ctx, project, ("write:system",))
    return _dumps(narration_ops.reactivate_request(project, event_id, actor=auth.actor(principal),
                                                   action=action, reason=reason))


@mcp.tool()
def doc_search(query: str, project: str = "maxwell") -> str:
    """Search the selected project's segmented corpus and return cited snippets: [{file, text}]."""
    hits = rag.search(query, top_k=5, project=project)
    return _dumps([{"file": h["file"], "text": h["text"]} for h in hits]) if hits else "no matches"


@mcp.tool()
def get_working_agreement(project: str = "maxwell") -> str:
    """Connect-time policy for agents: definition of done, branch convention, merge strategy,
    canonical main SHA, and the session-start sequence. Call before register_agent."""
    from switchboard.application.queries.working_agreement import execute
    return _dumps(execute(project=project))


@mcp.tool()
def ask_plan(question: str, project: str = "maxwell") -> str:
    """Queue a project-native plan-agent run and return immediately.

    Poll get_background_job_run(project, run_id) until status is completed or failed.
    The completed step result contains answer, sources, and confirmable task proposals.
    """
    selected = project_contract_service.resolve_project_input(project) or store.DEFAULT_PROJECT
    if not store.has_project(selected):
        return _dumps({"error": "unknown_project", "project": project})
    run = store.enqueue_background_job(
        project=selected,
        job_name="plan_agent_run",
        params={"question": question, "history": [], "record_chat": False},
        actor="mcp/ask_plan",
    )
    return _dumps({
        "run_id": run["run_id"],
        "project": selected,
        "status": "pending",
        "poll_with": "get_background_job_run",
    })


# Runner / external-effects / CI / merge_gate MCP tools live in
# switchboard.mcp.tools.runner and switchboard.mcp.tools.external_effects
# (ARCH-MS-67).

@mcp.tool()
def run_background_job(ctx: Context, job_name: str, project: str = "maxwell",
                       run_id: str = "", resume: bool = True,
                       params_json: str = "{}") -> str:
    """Run or resume a checkpointed background job (replay, audit export, receipts, reconcile)."""
    project = project_contract_service.resolve_project_input(project)
    principal = _require_write(ctx, project, ("write:ixp",))
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as exc:
        return _dumps({"error": "invalid params_json", "detail": str(exc)})
    if not isinstance(params, dict):
        return _dumps({"error": "params_json must decode to an object"})
    try:
        import background_jobs
        return _dumps(store.run_background_job(
            project=project,
            job_name=job_name,
            run_id=run_id,
            resume=resume,
            params=params,
            actor=auth.actor(principal),
        ))
    except background_jobs.JobBoundaryError as exc:
        return _dumps({"error": "job_boundary", "detail": str(exc)})


@mcp.tool()
def get_background_job_run(ctx: Context, run_id: str, project: str = "maxwell") -> str:
    """Fetch one persisted run; reconnecting resumes a non-terminal checkpoint."""
    project = project_contract_service.resolve_project_input(project)
    manifest = store.get_background_job_run(project=project, run_id=run_id)
    if manifest.get("status") in ("pending", "running"):
        store.ensure_background_job_running(
            project=project, run_id=run_id, actor="mcp/background_job/resume")
    return _dumps(manifest)


@mcp.tool()
def list_background_job_runs(ctx: Context, project: str = "maxwell",
                             job_name: str = "", limit: int = 20) -> str:
    """List recent checkpointed background job runs."""
    project = project_contract_service.resolve_project_input(project)
    return _dumps(store.list_background_job_runs(
        project=project, job_name=job_name, limit=limit))


@mcp.tool()
def reconcile(project: str = "maxwell") -> str:
    """Run the local board/git-provenance drift report. This first pass catches board-internal
    contradictions such as Done without merged_sha or In Review without PR/branch evidence."""
    return _dumps(store.reconcile(project=project))


@mcp.tool()
def reconcile_alerts(ctx: Context, project: str = "maxwell",
                     alert_to: str = "switchboard/operator",
                     min_severity: str = "medium",
                     requires_ack: bool = False) -> str:
    """Run the scheduled reconcile alert path now: reconcile, filter actionable findings,
    dedupe inside the configured window, and emit a directed agent message when needed.

    Reconcile alerts are fire-and-forget by default (requires_ack=false) so the ack inbox
    stays reserved for coordinator/agent handoffs. Legacy reconcile_alert backlog is
    auto-closed on each run."""
    _require_write(ctx, project, ("write:ixp",))
    return _dumps(store.run_reconcile_alerts(
        project=project, alert_to=alert_to, min_severity=min_severity,
        requires_ack=requires_ack))


# ---- directed agent IM (IXP write-authenticated) -----------------------
# send_agent_message / ack_message live in switchboard.mcp.tools.messaging
# (registered above). Read-side inbox helpers remain here until extracted.


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
def list_monitors(project: str = "maxwell", status: str = "", kind: str = "",
                  task_id: str = "") -> str:
    """List durable Switchboard monitors. status can be pending|fired|resolved|cancelled;
    kind can be ack_deadline. task_id narrows the result to one task."""
    return _dumps(store.list_coordination_monitors(
        status=status, kind=kind, task_id=task_id, project=project))


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


# ---- task write tools (Switchboard bearer-principal authenticated) -------
@mcp.tool()
def create_project(name: str, ctx: Context, project_id: str = "", label: str = "",
                   pretitle: str = "", github_repo: str = "",
                   purpose: str = "", boundary: str = "",
                   org_id: str = "", visibility: str = "private") -> str:
    """Create a new isolated project board and make it routable by all board tools.

    Authenticates against project='switchboard' with write:projects (contributors and up).
    `name` is the human name; `project_id` is optional and defaults to a lowercase slug, e.g.
    name='Vulkan' creates project='vulkan'. `github_repo` is optional owner/repo provenance
    config, e.g. github_repo='StevenRidder/Helm'. `visibility` is 'private' (default — only
    the creator, invitees, and org admins see it) or 'org' (all org members). Returns the
    created/existing project record.
    """
    principal = _require_write(ctx, "switchboard", ("write:projects",))
    result = store.create_project(name=name, project_id=project_id, label=label,
                                  pretitle=pretitle, github_repo=github_repo,
                                  owner_principal_id=principal["id"],
                                  org_id=org_id or store.DEFAULT_ORG_ID,
                                  purpose=purpose, boundary=boundary,
                                  visibility=(visibility or "private").strip().lower(),
                                  actor=auth.actor(principal))
    return _dumps(result)


@mcp.tool()
def set_project_github_repo(repo: str, ctx: Context, project: str = "maxwell") -> str:
    """Set the GitHub owner/repo used by reconcile to verify PR merge provenance for a board.

    Use this when a project board maps to a different repository than Switchboard itself, e.g.
    project='helm' -> repo='StevenRidder/Helm'. Requires system write scope because it changes
    the board's trust boundary for Done stamping.
    """
    principal = _require_write(ctx, "switchboard", ("write:system",))
    result = store.set_project_github_repo(repo=repo, project=project)
    if not result.get("error"):
        store.append_activity("project.github_repo_configured", auth.actor(principal),
                              {"project": project, "github_repo": repo},
                              task_id=None, project=project)
    return _dumps(result)


@mcp.tool()
def set_project_repo_topology(ctx: Context, project: str = "maxwell",
                              canonical_repo: str = "", public_ci_repo: str = "",
                              public_repo: str = "", release_repo: str = "",
                              topology_type: str = "",
                              canonical_default_branch: str = "",
                              canonical_claim_gate: str = "",
                              public_ci_required_status_contexts: str = "",
                              public_ci_sync_scripts: str = "",
                              public_publish_scripts: str = "",
                              release_publish_scripts: str = "",
                              ci_repo: str = "", ci_required_status_contexts: str = "",
                              ci_sync_scripts: str = "") -> str:
    """Configure first-class repository roles for a project.

    canonical_repo is the only code-truth / Done authority. public_ci_repo is a
    shared public CI sandbox for verification evidence only. public_repo and
    release_repo are publication/release evidence roles only. canonical_claim_gate
    sets off|warn|enforce for the SESSION-12 fleet PR provenance gate on that repo.
    ci_* arguments are accepted as aliases for public_ci_* during migration.
    """
    principal = _require_write(ctx, "switchboard", ("write:system",))
    result = store.set_project_repo_topology(
        project=project,
        canonical_repo=canonical_repo,
        public_ci_repo=public_ci_repo,
        public_repo=public_repo,
        release_repo=release_repo,
        topology_type=topology_type,
        canonical_default_branch=canonical_default_branch,
        canonical_claim_gate=canonical_claim_gate,
        public_ci_required_status_contexts=public_ci_required_status_contexts,
        public_ci_sync_scripts=public_ci_sync_scripts,
        public_publish_scripts=public_publish_scripts,
        release_publish_scripts=release_publish_scripts,
        ci_repo=ci_repo,
        ci_required_status_contexts=ci_required_status_contexts,
        ci_sync_scripts=ci_sync_scripts,
    )
    if not result.get("error"):
        store.append_activity("project.repo_topology_configured", auth.actor(principal),
                              {"project": project, "repo_topology": result.get("repo_topology")},
                              task_id=None, project=project)
    return _dumps(result)


@mcp.tool()
def create_project_board(title: str, ctx: Context, project: str = "maxwell",
                         board_id: str = "", mission_id: str = "",
                         kind: str = "mission", status: str = "active",
                         purpose: str = "", end_state: str = "",
                         description: str = "", owner_org: str = "",
                         owner_person_or_role: str = "",
                         metadata_json: str = "") -> str:
    """Create/update a first-class Board/Mission child under one Project.

    Project remains the repo/trust/policy/access/CI/model/budget/Done boundary.
    Boards/Missions are live outcome cockpits under that Project. Unknown projects fail closed.
    """
    principal = _require_write(ctx, project, ("write:tasks",))
    result = store.create_project_board({
        "id": board_id or mission_id,
        "title": title,
        "kind": kind,
        "status": status,
        "purpose": purpose,
        "end_state": end_state,
        "description": description,
        "owner_org": owner_org,
        "owner_person_or_role": owner_person_or_role,
        "metadata": metadata_json,
    }, actor=auth.actor(principal), project=project)
    return _dumps(result)


@mcp.tool()
def get_project_board(board_id: str, project: str = "maxwell") -> str:
    """Fetch one Board/Mission child by id from one Project."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    result = store.get_project_board(board_id, project=project)
    return _dumps(result or {"error": "unknown board", "board_id": board_id, "project": project})


@mcp.tool()
def list_project_boards(project: str = "maxwell", kind: str = "",
                        status: str = "") -> str:
    """List Board/Mission children under one Project, optionally filtered by kind/status."""
    if not store.has_project(project):
        return _dumps({"error": f"unknown project: {project}", "project": project})
    return _dumps({"project": project, "boards": store.list_project_boards(
        project=project, kind=kind, status=status)})



@mcp.tool()
def list_scoped_tokens(ctx: Context, project: str = "maxwell",
                       include_revoked: bool = False, kind: str = "") -> str:
    """List bearer principals for one project without exposing token hashes or raw tokens.

    Requires write:system on the target project. Use this to audit which humans, agents,
    hosts, or system actors can call Switchboard over REST/MCP.
    """
    _require_write(ctx, project, ("write:system",))
    return _dumps({
        "project": project,
        "tokens": store.list_principals(project=project, include_revoked=include_revoked, kind=kind),
        "scope_definitions": store.principal_scope_definitions(),
        "valid_kinds": sorted(store.VALID_PRINCIPAL_KINDS),
    })


@mcp.tool()
def get_audit_export(ctx: Context, project: str = "maxwell") -> str:
    """Return the redacted enterprise audit evidence bundle for one project.

    Requires write:system. The bundle includes task/activity history, claims, messages,
    runner/session/control evidence, Git/offline provenance, Tally economics, and access
    principal/role history without exposing token hashes or raw secrets.
    """
    _require_write(ctx, project, ("write:system",))
    from switchboard.application.queries.audit_export import execute
    return _dumps(execute(project=project))


@mcp.tool()
def list_cleanup_candidates(ctx: Context, project: str = "maxwell",
                            kinds: str = "", proof_task_age_days: float = 14) -> str:
    """List stale lifecycle cleanup candidates without changing board state.

    Requires write:system. Candidates cover stale agent presence, expired runner sessions,
    orphan/expired claims and leases, old wake intents, fired/orphan monitors, and old terminal
    proof/sentinel tasks that can be archived without deleting provenance.
    """
    _require_write(ctx, project, ("write:system",))
    return _dumps(store.cleanup_candidates(
        project=project,
        proof_task_age_days=proof_task_age_days,
        include_kinds=store.coerce_csv_list(kinds),
    ))


@mcp.tool()
def apply_cleanup(ctx: Context, project: str = "maxwell", candidate_ids: str = "",
                  dry_run: bool = True, reason: str = "operator lifecycle cleanup",
                  kinds: str = "", proof_task_age_days: float = 14) -> str:
    """Dry-run or apply safe lifecycle cleanup with an audit trail.

    Pass comma/newline-separated candidate_ids to limit scope. With dry_run=false, each mutation
    writes cleanup activity and uses archive/resolve paths rather than raw deletion.
    """
    principal = _require_write(ctx, project, ("write:system",))
    return _dumps(store.apply_cleanup(
        project=project,
        candidate_ids=store.coerce_csv_list(candidate_ids),
        dry_run=dry_run,
        actor=auth.actor(principal),
        reason=reason,
        proof_task_age_days=proof_task_age_days,
        include_kinds=store.coerce_csv_list(kinds),
    ))


def _scoped_token_auth_project(project: str) -> str:
    binding = (project or "maxwell").strip()
    if store.is_global_project_binding(binding):
        return "switchboard"
    return binding


@mcp.tool()
def create_scoped_token(ctx: Context, project: str = "maxwell", kind: str = "agent",
                        display_name: str = "", scopes: str = "", role: str = "",
                        principal_id: str = "") -> str:
    """Create one project-scoped bearer token for REST/MCP callers.

    Requires write:system on the target project. Pass project='*' for a global agent token that
    can read/write every current and future board. `role` is a preset such as viewer,
    contributor, operator, or admin; `scopes` can also be a comma/newline list. The raw token is
    returned once and is never stored, so capture it immediately.
    """
    binding = (project or "maxwell").strip()
    auth_project = _scoped_token_auth_project(binding)
    if not store.is_global_project_binding(binding) and not store.has_project(binding):
        return _dumps({"error": f"unknown project: {binding}"})
    principal = _require_write(ctx, auth_project, ("write:system",))
    resolved = store.resolve_principal_scopes(scopes, role=role)
    if resolved.get("error"):
        return _dumps(resolved)
    normalized_kind = store.validate_principal_kind(kind or "agent")
    if not normalized_kind:
        return _dumps({"error": "kind must be one of: " + ", ".join(sorted(store.VALID_PRINCIPAL_KINDS))})
    raw_token = auth.new_secret_token()
    created = store.create_principal(
        kind=normalized_kind,
        display_name=display_name or normalized_kind,
        token=raw_token,
        scopes=resolved["scopes"],
        principal_id=principal_id or None,
        project=binding,
    )
    if created.get("error"):
        return _dumps(created)
    public = store.public_principal_record(created, project=auth_project)
    store.append_activity(
        "access.token_created",
        auth.actor(principal),
        {"principal": public, "role": resolved.get("role"), "token_returned_once": True},
        task_id=None,
        project=auth_project,
    )
    return _dumps({"project": binding, "principal": public, "token": raw_token,
                   "token_returned_once": True})


@mcp.tool()
def revoke_scoped_token(principal_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Revoke one project-scoped bearer principal and any live sessions for that principal."""
    principal = _require_write(ctx, project, ("write:system",))
    result = store.revoke_principal_token(principal_id, project=project, actor=auth.actor(principal))
    return _dumps(result)


@mcp.tool()
def submit_bug(source_task: str, observed_behavior: str, expected_behavior: str,
               repro_steps: str, evidence: str, severity_hint: str,
               affected_surface: str, ctx: Context, project: str = "maxwell",
               source_agent: str = "", failure_class: str = "",
               duplicate_of: str = "", title: str = "") -> str:
    """Submit an agent-discovered bug through the dedicated BUG intake path.

    Requires write:bug_intake. Creates exactly one BUG triage task with structured
    bug_report state and source task/agent linkage. It does not create implementation
    work, mark work Ready, dispatch agents, or bypass the human gate.
    """
    principal = _require_write(ctx, project, ("write:bug_intake",))
    actor_name = auth.actor(principal)
    from switchboard.application.commands.submit_bug import execute_mapping_result
    result = execute_mapping_result({
        "source_task": source_task,
        "source_agent": source_agent or actor_name,
        "observed_behavior": observed_behavior,
        "expected_behavior": expected_behavior,
        "repro_steps": repro_steps,
        "evidence": evidence,
        "severity_hint": severity_hint,
        "affected_surface": affected_surface,
        "failure_class": failure_class,
        "duplicate_of": duplicate_of,
        "title": title,
    }, actor=actor_name, project=project)
    return _dumps(result)


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
def dispatch_to_claude_code(task_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Queue a task for autonomous development via the fleet. Enqueues a lane-scoped claim_next
    wake intent that a work-capable Agent Host claims and runs in an isolated worktree, opening a
    PR on a `claude/<task>` branch — never main. Returns {dispatched, wake_id, work_hosts_online, …}.
    If no work-capable host is online for the task's project/lane, the wake queues until one is
    (deploy/switchboard-agent-host-work.service.example). project selects the board."""
    principal = _require_write(ctx, project)
    import dispatch as dispatch_mod
    return _dumps(dispatch_mod.dispatch(task_id, actor=auth.actor(principal), project=project))


@mcp.tool()
def dispatch_to_codex_cloud(task_id: str, ctx: Context,
                            project: str = "maxwell") -> str:
    """Queue a task for OpenAI-hosted Codex cloud execution.

    The eligible bridge host uses the official ``codex cloud exec`` command, then binds the
    app-visible ChatGPT/Codex task URL to the wake and runner-session registry. Dispatch fails
    visibly when Codex auth, the cloud environment, canonical-repo grant, scoped MCP bridge, or
    agent internet allowlist is absent; it never substitutes local ``codex exec`` compute.
    """
    principal = _require_write(ctx, project)
    import dispatch as dispatch_mod
    return _dumps(dispatch_mod.dispatch(
        task_id, actor=auth.actor(principal), project=project, runtime="codex"))


@mcp.tool()
def dispatch_to_co_fleet(task_id: str, runtime_config_ref: str, ctx: Context,
                         project: str = "switchboard", runtime: str = "claude-code",
                         capabilities: str = "", allow_on_demand: bool = False,
                         account_binding_json: str = "") -> str:
    """Queue a task for zero-to-one elastic CO Fleet capacity.

    ``runtime_config_ref`` must be an SSM/Secrets Manager reference, never a token.
    Optional ``account_binding_json`` carries the non-secret BYOA account-affinity
    contract; incomplete or inconsistent bindings fail closed. On-Demand is an
    explicit infrastructure fallback only and never authorizes metered model/API use.
    """
    principal = _require_write(ctx, project)
    try:
        account_binding = json.loads(account_binding_json) if account_binding_json else None
    except json.JSONDecodeError as exc:
        return _dumps({"dispatched": False, "error": "invalid_account_binding",
                       "reason": f"invalid JSON: {exc.msg}"})
    import dispatch as dispatch_mod
    return _dumps(dispatch_mod.dispatch_to_co_fleet(
        task_id, actor=auth.actor(principal), project=project, runtime=runtime,
        capabilities=[item.strip() for item in capabilities.split(",") if item.strip()],
        runtime_config_ref=runtime_config_ref, allow_on_demand=allow_on_demand,
        account_binding=account_binding))


@mcp.tool()
def ingest_and_triage(kind: str, title: str, text: str, ctx: Context, project: str = "maxwell") -> str:
    """Ingest an artifact (email / transcript / document / note) into `project`'s RAG corpus AND
    triage it against that board. Returns {summary, proposals, new_tasks, sources} — proposals are
    NOT applied (use update_task / create_task to apply). kind: email|transcript|document|note.
    project selects the board — the corpus is segmented per project."""
    _require_write(ctx, project)
    return _dumps(intake_mod.ingest_and_triage(kind, title, text, project=project))


if __name__ == "__main__":
    import uvicorn

    # NARRATE-14: register the event-driven narration wake accelerator in the MCP process too.
    # Agents mutate the board through this process, so their post-commit emits must accelerate here
    # (otherwise their narration would only be picked up by the ~5min recovery sweep, missing the
    # <=60s freshness SLO). Inert until PM_NARRATION_EVENT_PRIMARY is set; the durable outbox +
    # narrate_events sweep remain the backstop, so a missed wake never loses work.
    try:
        import narration_cutover
        narration_cutover.register_production_wake_sink()
    except Exception as _e:  # never let narration wiring block the control plane from starting
        print(f"[narration] wake sink registration skipped: {_e}")

    # FastMCP.run() builds this same ASGI app internally. Running it explicitly
    # lets Switchboard attach timing/reconnect headers to success and error paths.
    # Timing wraps auth so even rejected (401) requests carry timing headers; auth wraps
    # the tool app so anonymous callers are turned away before any tool runs (BUG-46).
    from concurrency_limiter import ConcurrencyLimitASGIMiddleware

    # The observability endpoint sits between timing and auth (HARDEN-63): operators
    # scrape GET /observability without a token (read-only, no request content), and the
    # response still carries the standard server-timing header.
    app = MCPServerTimingMiddleware(
        MCPObservabilityEndpoint(
            MCPAuthMiddleware(
                ConcurrencyLimitASGIMiddleware(mcp.streamable_http_app()),
            ),
            _mcp_observability.snapshot,
        )
    )
    uvicorn.run(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
