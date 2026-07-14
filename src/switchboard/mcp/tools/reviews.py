"""MCP adapter for durable code-review verdict commands and queries (COORD-18)."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from mcp.server.fastmcp import Context

from switchboard.application.commands import review_verdicts as review_commands
from switchboard.application.queries import review_verdicts as review_queries


@dataclass(frozen=True)
class ReviewToolServices:
    dumps: Callable[[Any], str]
    require_write: Callable[..., dict[str, Any]]
    resolve_write_actor: Callable[..., dict[str, Any]]
    write_binding_comment: Callable[..., None]


_SERVICES: ReviewToolServices | None = None


def _services() -> ReviewToolServices:
    if _SERVICES is None:
        raise RuntimeError("review MCP tools must be registered before use")
    return _SERVICES


def record_review_verdict(verdict_json: str, ctx: Context,
                          project: str = "maxwell") -> str:
    """Persist one independent code-review verdict for the task's exact current PR head.

    verdict_json follows switchboard.review_verdict.record_command.v1 and includes
    task_id, pr_url, head_sha, reviewer_principal, status=pass|changes_requested,
    and findings[]. The authenticated reviewer must be a different principal from
    every recorded worker for the task.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:ixp",))
    try:
        payload = json.loads(verdict_json or "{}")
    except json.JSONDecodeError:
        return services.dumps({"error": "verdict_json must be valid JSON"})
    if not isinstance(payload, dict):
        return services.dumps({"error": "verdict_json must be a JSON object"})
    task_id = str(payload.get("task_id") or "").strip()
    reviewer = str(payload.get("reviewer_principal") or "").strip()
    binding = services.resolve_write_actor(
        principal, project=project, task_id=task_id, agent_id=reviewer)
    if not binding.get("ok"):
        return services.dumps(binding)
    payload["reviewer_principal"] = binding["actor"]
    result = review_commands.execute_mapping(
        payload, actor=binding["actor"], principal_id=principal.get("id") or "",
        project=project,
    )
    if result.get("created"):
        services.write_binding_comment(task_id, binding, project)
    return services.dumps(result)


def get_review_verdict(task_id: str, head_sha: str = "",
                       project: str = "maxwell") -> str:
    """Read the verdict for one task/head; omitted head_sha means the current task head."""
    services = _services()
    verdict = review_queries.get_for(task_id, project=project, head_sha=head_sha)
    return services.dumps(verdict or {
        "error": "review_verdict_not_found",
        "task_id": task_id,
        "head_sha": head_sha or None,
    })


def list_review_findings(task_id: str, project: str = "maxwell", head_sha: str = "",
                         state: str = "", finding_class: str = "",
                         severity: str = "", current_head_only: bool = False) -> str:
    """List queryable code-review findings with exact-head validity and resolution state."""
    services = _services()
    findings = review_queries.list_findings_for(
        task_id, project=project, head_sha=head_sha, state=state,
        finding_class=finding_class, severity=severity,
        current_head_only=current_head_only,
    )
    return services.dumps({
        "task_id": task_id,
        "finding_count": len(findings),
        "findings": findings,
    })


def resolve_review_finding(resolution_json: str, ctx: Context,
                           project: str = "maxwell") -> str:
    """Admin-authorized open -> waived|overridden transition for one exact-head finding."""
    services = _services()
    principal = services.require_write(ctx, project, ("admin",))
    try:
        payload = json.loads(resolution_json or "{}")
    except json.JSONDecodeError:
        return services.dumps({"error": "resolution_json must be valid JSON"})
    if not isinstance(payload, dict):
        return services.dumps({"error": "resolution_json must be a JSON object"})
    task_id = str(payload.get("task_id") or "").strip()
    resolver = str(payload.get("resolver_principal") or "").strip()
    binding = services.resolve_write_actor(
        principal, project=project, task_id=task_id, agent_id=resolver)
    if not binding.get("ok"):
        return services.dumps(binding)
    payload["resolver_principal"] = binding["actor"]
    result = review_commands.resolve_finding_mapping(
        payload, actor=binding["actor"], principal_id=principal.get("id") or "",
        authorized=True, project=project,
    )
    if result.get("resolved"):
        services.write_binding_comment(task_id, binding, project)
    return services.dumps(result)


REVIEW_TOOL_NAMES = (
    "record_review_verdict",
    "get_review_verdict",
    "list_review_findings",
    "resolve_review_finding",
)


def register_review_tools(mcp: Any, services: ReviewToolServices) -> dict[str, Callable[..., str]]:
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in REVIEW_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
