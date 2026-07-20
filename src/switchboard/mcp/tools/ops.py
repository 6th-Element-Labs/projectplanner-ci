"""Operator/agent-ops MCP tools: bug intake, digest, notify, fleet dispatch,
and RAG ingest-and-triage (ARCH-MS-70).

Transport adapter extracted from ``mcp_server_impl``. Authentication and MCP
serialization remain edge concerns; the shared ``digest``/``notify``/``intake``/
``dispatch`` modules own the actual policy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import digest as digest_mod
import intake as intake_mod
import notify as notify_mod


@dataclass(frozen=True)
class OpsToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: OpsToolServices | None = None


def _services() -> OpsToolServices:
    if _SERVICES is None:
        raise RuntimeError("ops MCP tools must be registered before use")
    return _SERVICES


def submit_bug(source_task: str, observed_behavior: str, expected_behavior: str,
               repro_steps: str, evidence: str, severity_hint: str,
               affected_surface: str, ctx: Context, project: str = "maxwell",
               source_agent: str = "", failure_class: str = "",
               duplicate_of: str = "", title: str = "") -> str:
    """Submit an agent-discovered bug through the dedicated BUG intake path.

    Requires write:bug_intake. Creates exactly one BUG triage task with structured
    bug_report state and source task/agent linkage. It does not create implementation
    work by itself; audited Autopilot routing is a separate lifecycle step.
    """
    from switchboard.application.commands.submit_bug import execute_mapping_result
    services = _services()
    principal = services.require_write(ctx, project, ("write:bug_intake",))
    actor_name = auth.actor(principal)
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
    return services.dumps(result)


def generate_digest(ctx: Context, project: str = "maxwell") -> str:
    """Generate + post the weekly chief-of-staff brief (plan signals + activity deltas since the
    last digest). Returns the brief text. Creates a digest record."""
    services = _services()
    if project != "maxwell":
        return services.dumps({
            "error": "project_not_supported",
            "project": project,
            "message": "generate_digest is still a Maxwell-only compatibility adapter",
        })
    services.require_write(ctx, project)
    return digest_mod.generate_digest().get("content", "")


def notify(subject: str, text: str, ctx: Context, project: str = "maxwell") -> str:
    """Send a message to the wired channels (Slack + Email). Unconfigured channels are dry-run."""
    services = _services()
    if project != "maxwell":
        return services.dumps({
            "error": "project_not_supported",
            "project": project,
            "message": "notify is still a Maxwell-only compatibility adapter",
        })
    services.require_write(ctx, project)
    return services.dumps(notify_mod.send(subject, text))


def dispatch_to_claude_code(task_id: str, ctx: Context, project: str = "maxwell") -> str:
    """Queue a task for autonomous development via the fleet. Enqueues a lane-scoped claim_next
    wake intent that a work-capable Agent Host claims and runs in an isolated worktree, opening a
    PR on a `claude/<task>` branch — never main. Returns {dispatched, wake_id, work_hosts_online, …}.
    If no work-capable host is online for the task's project/lane, the wake queues until one is
    (deploy/switchboard-agent-host-work.service.example). project selects the board."""
    services = _services()
    principal = services.require_write(ctx, project)
    import dispatch as dispatch_mod
    return services.dumps(dispatch_mod.dispatch(task_id, actor=auth.actor(principal), project=project))


def dispatch_to_codex_cloud(task_id: str, ctx: Context,
                            project: str = "maxwell") -> str:
    """Queue a task for OpenAI-hosted Codex cloud execution.

    The eligible bridge host uses the official ``codex cloud exec`` command, then binds the
    app-visible ChatGPT/Codex task URL to the wake and runner-session registry. Dispatch fails
    visibly when Codex auth, the cloud environment, canonical-repo grant, scoped MCP bridge, or
    agent internet allowlist is absent; it never substitutes local ``codex exec`` compute.
    """
    services = _services()
    principal = services.require_write(ctx, project)
    import dispatch as dispatch_mod
    return services.dumps(dispatch_mod.dispatch(
        task_id, actor=auth.actor(principal), project=project, runtime="codex"))


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
    services = _services()
    principal = services.require_write(ctx, project)
    try:
        account_binding = json.loads(account_binding_json) if account_binding_json else None
    except json.JSONDecodeError as exc:
        return services.dumps({"dispatched": False, "error": "invalid_account_binding",
                       "reason": f"invalid JSON: {exc.msg}"})
    import dispatch as dispatch_mod
    return services.dumps(dispatch_mod.dispatch_to_co_fleet(
        task_id, actor=auth.actor(principal), project=project, runtime=runtime,
        capabilities=[item.strip() for item in capabilities.split(",") if item.strip()],
        runtime_config_ref=runtime_config_ref, allow_on_demand=allow_on_demand,
        account_binding=account_binding))


def ingest_and_triage(kind: str, title: str, text: str, ctx: Context, project: str = "maxwell") -> str:
    """Ingest an artifact (email / transcript / document / note) into `project`'s RAG corpus AND
    triage it against that board. Returns {summary, proposals, new_tasks, sources} — proposals are
    NOT applied (use update_task / create_task to apply). kind: email|transcript|document|note.
    project selects the board — the corpus is segmented per project."""
    services = _services()
    services.require_write(ctx, project)
    return services.dumps(intake_mod.ingest_and_triage(kind, title, text, project=project))


# SIMPLIFY-10: ``start_task`` moved to switchboard.mcp.tools.task_execution with
# the rest of the command set, so one module owns the whole execution surface.


OPS_TOOL_NAMES = (
    "submit_bug", "generate_digest", "notify", "dispatch_to_claude_code",
    "dispatch_to_codex_cloud", "dispatch_to_co_fleet", "ingest_and_triage",
)


def register_ops_tools(mcp: Any, services: OpsToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the ops tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in OPS_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
