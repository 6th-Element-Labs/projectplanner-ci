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
               duplicate_of: str = "", title: str = "",
               review_repair_json: str = "") -> str:
    """Submit an agent-discovered bug through the dedicated BUG intake path.

    Requires write:bug_intake. Creates exactly one BUG task with structured report
    state and source linkage, then routes and starts its ordinary implementation
    lifecycle without a second identity handoff. Declared duplicates are linked but
    do not fork another implementation session.
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
        "review_repair_json": review_repair_json,
    }, actor=actor_name, principal_id=str(principal.get("id") or ""), project=project)
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
    "submit_bug", "generate_digest", "notify", "ingest_and_triage",
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
