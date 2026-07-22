"""Checkpointed background-job MCP tools (replay, audit export, receipts,
reconcile, plan-agent runs) — ARCH-MS-70.

Transport adapter extracted from ``mcp_server_impl``. Authentication and MCP
serialization remain edge concerns; persistence stays behind ``store``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import auth
import project_contract as project_contract_service
import store
from switchboard.mcp.authorization import require_current_access


@dataclass(frozen=True)
class BackgroundJobToolServices:
    """Monolith edge services injected while ``mcp_server`` remains the host."""

    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]


_SERVICES: BackgroundJobToolServices | None = None


def _services() -> BackgroundJobToolServices:
    if _SERVICES is None:
        raise RuntimeError("background job MCP tools must be registered before use")
    return _SERVICES


def run_background_job(ctx: Context, job_name: str, project: str = "maxwell",
                       run_id: str = "", resume: bool = True,
                       params_json: str = "{}") -> str:
    """Run or resume a checkpointed background job (replay, audit export, receipts, reconcile)."""
    services = _services()
    project = project_contract_service.resolve_project_input(project)
    required = ("use:llm",) if job_name == "plan_agent_run" else ("write:ixp",)
    principal = services.require_write(ctx, project, required)
    try:
        params = json.loads(params_json or "{}")
    except json.JSONDecodeError as exc:
        return services.dumps({"error": "invalid params_json", "detail": str(exc)})
    if not isinstance(params, dict):
        return services.dumps({"error": "params_json must decode to an object"})
    try:
        import background_jobs
        return services.dumps(store.run_background_job(
            project=project,
            job_name=job_name,
            run_id=run_id,
            resume=resume,
            params=params,
            actor=auth.actor(principal),
        ))
    except background_jobs.JobBoundaryError as exc:
        return services.dumps({"error": "job_boundary", "detail": str(exc)})


def get_background_job_run(ctx: Context, run_id: str, project: str = "maxwell") -> str:
    """Fetch one persisted run; reconnecting resumes a non-terminal checkpoint."""
    services = _services()
    project = project_contract_service.resolve_project_input(project)
    manifest = store.get_background_job_run(project=project, run_id=run_id)
    if manifest.get("status") in ("pending", "running"):
        if manifest.get("job_name") == "plan_agent_run":
            require_current_access(project, ("use:llm",))
        store.ensure_background_job_running(
            project=project, run_id=run_id, actor="mcp/background_job/resume")
    return services.dumps(manifest)


def list_background_job_runs(ctx: Context, project: str = "maxwell",
                             job_name: str = "", limit: int = 20) -> str:
    """List recent checkpointed background job runs."""
    services = _services()
    project = project_contract_service.resolve_project_input(project)
    return services.dumps(store.list_background_job_runs(
        project=project, job_name=job_name, limit=limit))


BACKGROUND_JOB_TOOL_NAMES = (
    "run_background_job", "get_background_job_run", "list_background_job_runs",
)


def register_background_job_tools(
        mcp: Any, services: BackgroundJobToolServices) -> dict[str, Callable[..., str]]:
    """Configure and register the background-job tool set on one FastMCP host."""
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in BACKGROUND_JOB_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
