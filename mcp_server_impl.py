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
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import auth
import store
import scripts.switchboard_path  # noqa: F401
from mcp_observability import MCPObservability
from mcp_dispatch import MCPToolDispatcher
from mcp_http_timing import MCPServerTimingMiddleware
from mcp_observability_http import MCPObservabilityEndpoint
from mcp_auth import MCPAuthMiddleware
from switchboard.mcp import deps
from switchboard.mcp.tools import access as access_tools
from switchboard.mcp.tools import background_jobs as background_job_tools
from switchboard.mcp.tools import board as board_tools
from switchboard.mcp.tools import boot as boot_tools
from switchboard.mcp.tools import decisions as decision_tools
from switchboard.mcp.tools import monitors as monitor_tools
from switchboard.mcp.tools import narration as narration_tools
from switchboard.mcp.tools import observability as observability_tools
from switchboard.mcp.tools import ops as ops_tools
from switchboard.mcp.tools import plan as plan_tools
from switchboard.mcp.tools import projects as project_tools
from switchboard.mcp.tools import provider_credentials as provider_credential_tools
from switchboard.mcp.tools import reconcile as reconcile_tools
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

# HARDEN-63: deps.require_write feeds the write-latency histogram on the same
# observability singleton wrap()/snapshot() use, without deps.py owning it.
deps.register_write_observer(_mcp_observability.mark_write)

_dumps = deps.dumps
_require_write = deps.require_write
_require_read = deps.require_read
_resolve_write_actor = deps.resolve_write_actor
_write_binding_comment = deps.write_binding_comment


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

_monitor_tool_functions = monitor_tools.register_monitor_tools(
    mcp,
    monitor_tools.MonitorToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_monitor_tool_functions)

_narration_tool_functions = narration_tools.register_narration_tools(
    mcp,
    narration_tools.NarrationToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_narration_tool_functions)

_observability_tool_functions = observability_tools.register_observability_tools(
    mcp,
    observability_tools.ObservabilityToolServices(
        dumps=_dumps,
        observability=_mcp_observability,
    ),
)
globals().update(_observability_tool_functions)

_plan_tool_functions = plan_tools.register_plan_tools(
    mcp,
    plan_tools.PlanToolServices(dumps=_dumps),
)
globals().update(_plan_tool_functions)

_background_job_tool_functions = background_job_tools.register_background_job_tools(
    mcp,
    background_job_tools.BackgroundJobToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_background_job_tool_functions)

_reconcile_tool_functions = reconcile_tools.register_reconcile_tools(
    mcp,
    reconcile_tools.ReconcileToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_reconcile_tool_functions)

_access_tool_functions = access_tools.register_access_tools(
    mcp,
    access_tools.AccessToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_access_tool_functions)

_ops_tool_functions = ops_tools.register_ops_tools(
    mcp,
    ops_tools.OpsToolServices(
        dumps=_dumps,
        require_write=_require_write,
    ),
)
globals().update(_ops_tool_functions)


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
