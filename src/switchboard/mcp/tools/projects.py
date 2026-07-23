"""Access-controlled project lifecycle MCP read tools."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.fastmcp import Context

import store
from switchboard.application.commands import (
    project_consolidation,
    project_lifecycle,
    project_metadata,
    project_purge,
)
from switchboard.application.queries import project_admin, project_impact


@dataclass(frozen=True)
class ProjectToolServices:
    dumps: Callable[[Any], str]
    require_read: Callable[..., dict[str, Any]]
    require_write: Callable[..., dict[str, Any]]
    principal_actor: Callable[[dict[str, Any]], str]


_SERVICES: ProjectToolServices | None = None


def _services() -> ProjectToolServices:
    if _SERVICES is None:
        raise RuntimeError("project MCP tools must be registered before use")
    return _SERVICES


def get_project_impact_report(ctx: Context, project: str = "maxwell",
                              limit: int = 50) -> str:
    """Read-only project dependency/sprawl impact audit.

    Returns the versioned ``switchboard.project_impact_report.v1`` contract with
    bounded deterministic samples, archive blockers, and a keep/consolidate/archive
    recommendation. Requires read access to the selected project.
    """
    services = _services()
    services.require_read(ctx, project, ("read",))
    result = project_impact.execute_for(
        project,
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        limit=limit,
    )
    return services.dumps(result)


def get_project(ctx: Context, project: str = "maxwell") -> str:
    """Return the shared project administration record, access summary, and receipts."""
    services = _services()
    principal = services.require_read(ctx, project, ("read",))
    result = project_admin.execute_for(
        project,
        access_repository=store.access_repository,
        repo_topology_provider=store.get_project_repo_topology,
        access_model_provider=store.project_access_model,
        principal_id=str(principal.get("id") or ""),
        principal_scopes=list(
            principal.get("effective_scopes") or principal.get("scopes") or []),
    )
    return services.dumps(result)


def update_project(ctx: Context, project: str, metadata_json: str) -> str:
    """Update safe ordinary metadata; lifecycle and ownership fields are rejected."""
    services = _services()
    if not str(project or "").strip():
        return services.dumps({"error": "project is required"})
    try:
        metadata = json.loads(metadata_json or "")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "metadata_json must be valid JSON"})
    if not isinstance(metadata, dict):
        return services.dumps({"error": "metadata_json must decode to an object"})
    required = (("write:system",)
                if {"boundary", "visibility"}.intersection(metadata)
                else ("write:projects",))
    principal = services.require_write(ctx, project, required)
    result = project_metadata.execute(
        {**metadata, "project_id": project},
        actor=services.principal_actor(principal),
        access_repository=store.access_repository,
    )
    return services.dumps(result)


def archive_project(ctx: Context, project: str, reason: str,
                    impact_report_receipt_json: str) -> str:
    """Archive a project against an exact current impact-report receipt."""
    services = _services()
    if not str(project or "").strip():
        return services.dumps({"error": "project is required"})
    principal = services.require_write(ctx, project, ("write:system",))
    try:
        receipt = json.loads(impact_report_receipt_json or "")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "impact_report_receipt_json must be valid JSON"})
    result = project_lifecycle.archive_project(
        {"project_id": project, "reason": reason,
         "impact_report_receipt": receipt,
         "actor": services.principal_actor(principal)},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def restore_project(ctx: Context, project: str, reason: str) -> str:
    """Restore archived project writes after access and topology validation."""
    services = _services()
    if not str(project or "").strip():
        return services.dumps({"error": "project is required"})
    principal = services.require_write(ctx, project, ("write:system",))
    result = project_lifecycle.restore_project(
        {"project_id": project, "reason": reason,
         "actor": services.principal_actor(principal)},
        access_repository=store.access_repository,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def plan_project_consolidation(ctx: Context, project: str,
                               replacement_project: str, reason: str,
                               approval_json: str,
                               replacement_board: str = "",
                               replacement_mission: str = "",
                               replacement_deliverable: str = "",
                               safe_routing_keys_json: str = "[]") -> str:
    """Dry-run an operator-approved project consolidation; no state is mutated."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    try:
        approval = json.loads(approval_json or "")
        routing_keys = json.loads(safe_routing_keys_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "approval and routing keys must be valid JSON"})
    result = project_consolidation.plan_project_consolidation(
        {
            "source_project_id": project,
            "replacement_project_id": replacement_project,
            "replacement_board_id": replacement_board or None,
            "replacement_mission_id": replacement_mission or None,
            "replacement_deliverable_id": replacement_deliverable or None,
            "safe_routing_keys": routing_keys,
            "reason": reason,
            "actor": services.principal_actor(principal),
            "approval": approval,
        },
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def apply_project_consolidation(ctx: Context, project: str,
                                plan_json: str, confirmation: str) -> str:
    """Apply an exact current consolidation plan and immediately verify it."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    try:
        plan = json.loads(plan_json or "")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "plan_json must be valid JSON"})
    if not isinstance(plan, dict) or plan.get("source_project_id") != project:
        return services.dumps({"error": "plan source does not match project"})
    result = project_consolidation.apply_project_consolidation(
        {"plan": plan, "confirmation": confirmation,
         "actor": services.principal_actor(principal)},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def verify_project_consolidation(ctx: Context, project: str,
                                 consolidation_id: str) -> str:
    """Verify archived source history, pointers, routes, and cross-project graph reads."""
    services = _services()
    services.require_read(ctx, project, ("read",))
    result = project_consolidation.verify_project_consolidation(
        project, consolidation_id,
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    return services.dumps(result)


def rollback_project_consolidation(ctx: Context, project: str,
                                   consolidation_id: str, reason: str) -> str:
    """Rollback a consolidation before purge and restore exact routing state."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    result = project_consolidation.rollback_project_consolidation(
        {"source_project_id": project, "consolidation_id": consolidation_id,
         "reason": reason, "actor": services.principal_actor(principal)},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
    )
    return services.dumps(result)


def create_project_purge_intent(ctx: Context, project: str, reason: str,
                                retention_days: int, export_evidence_json: str,
                                typed_confirmation: str) -> str:
    """Prepare a guarded purge intent; this never deletes project data."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    try:
        evidence = json.loads(export_evidence_json or "")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "export_evidence_json must be valid JSON"})
    return services.dumps(project_purge.create_purge_intent(
        {"project_id": project, "reason": reason, "retention_days": retention_days,
         "export": evidence, "typed_confirmation": typed_confirmation,
         "actor": services.principal_actor(principal)},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology))


def verify_project_purge_intent(ctx: Context, project: str, intent_id: str,
                                typed_confirmation: str) -> str:
    """Perform the required second, current-state verification for a purge intent."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    return services.dumps(project_purge.verify_purge_intent(
        {"project_id": project, "intent_id": intent_id,
         "typed_confirmation": typed_confirmation,
         "verifier": services.principal_actor(principal)},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology))


def execute_project_purge(ctx: Context, project: str, intent_id: str,
                          explicit_authorization: str) -> str:
    """Execute an independently authorized, verified purge and retain its tombstone."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    return services.dumps(project_purge.execute_purge(
        {"project_id": project, "intent_id": intent_id,
         "explicit_authorization": explicit_authorization,
         "actor": services.principal_actor(principal)},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology))


def record_project_cleanup_review(ctx: Context, project: str, decision: str,
                                  impact_report_receipt_json: str, approved_at: float,
                                  rationale: str) -> str:
    """Persist an operator-reviewed keep/consolidate/archive recommendation only."""
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    try:
        receipt = json.loads(impact_report_receipt_json or "")
    except (TypeError, json.JSONDecodeError):
        return services.dumps({"error": "impact_report_receipt_json must be valid JSON"})
    actor = services.principal_actor(principal)
    return services.dumps(project_purge.record_cleanup_review(
        {"project_id": project, "decision": decision,
         "impact_report_receipt": receipt, "approved_by": actor,
         "approved_at": approved_at, "rationale": rationale},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology))


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
    services = _services()
    principal = services.require_write(ctx, "switchboard", ("write:projects",))
    result = store.create_project(name=name, project_id=project_id, label=label,
                                  pretitle=pretitle, github_repo=github_repo,
                                  owner_principal_id=principal["id"],
                                  org_id=org_id or store.DEFAULT_ORG_ID,
                                  purpose=purpose, boundary=boundary,
                                  visibility=(visibility or "private").strip().lower(),
                                  actor=services.principal_actor(principal))
    return services.dumps(result)


def set_project_github_repo(repo: str, ctx: Context, project: str = "maxwell") -> str:
    """Set the GitHub owner/repo used by reconcile to verify PR merge provenance for a board.

    Use this when a project board maps to a different repository than Switchboard itself, e.g.
    project='helm' -> repo='StevenRidder/Helm'. Requires system write scope because it changes
    the board's trust boundary for Done stamping.
    """
    services = _services()
    principal = services.require_write(ctx, "switchboard", ("write:system",))
    result = store.set_project_github_repo(repo=repo, project=project)
    if not result.get("error"):
        store.append_activity("project.github_repo_configured", services.principal_actor(principal),
                              {"project": project, "github_repo": repo},
                              task_id=None, project=project)
    return services.dumps(result)


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
    services = _services()
    principal = services.require_write(ctx, "switchboard", ("write:system",))
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
        store.append_activity("project.repo_topology_configured", services.principal_actor(principal),
                              {"project": project, "repo_topology": result.get("repo_topology")},
                              task_id=None, project=project)
    return services.dumps(result)


def get_project_execution_policy(ctx: Context, project: str = "maxwell") -> str:
    """Read the project execution policy every runner must satisfy.

    Returns ``switchboard.project_execution_policy.v1``: allowed runtimes, workspace
    repo role and isolation, host classes, trust zones, burst policy, provider
    selectors, SCM connection reference, Autopilot enablement, lifecycle/versioning,
    and a typed ``readiness`` gate that fails closed when policy is missing or invalid.
    """
    services = _services()
    services.require_read(ctx, project, ("read",))
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    return services.dumps(store.get_project_execution_policy(project=project))


def set_project_execution_policy(ctx: Context, project: str = "maxwell",
                                 policy_json: str = "") -> str:
    """Merge an execution-policy update for one project (write:system on that project).

    ``policy_json`` is a partial ``switchboard.project_execution_policy.v1`` body;
    nested objects merge and lists replace. References and policy only — branch,
    env, and secret fields are rejected, and an invalid merged result persists nothing.
    """
    services = _services()
    principal = services.require_write(ctx, project, ("write:system",))
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    try:
        updates = json.loads(policy_json or "{}")
    except json.JSONDecodeError as exc:
        return services.dumps({"error": f"policy_json must be valid JSON: {exc}",
                               "project": project})
    if not isinstance(updates, dict):
        return services.dumps({"error": "policy_json must be a JSON object",
                               "project": project})
    return services.dumps(store.set_project_execution_policy(
        project=project, updates=updates, actor=services.principal_actor(principal)))


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
    services = _services()
    principal = services.require_write(ctx, project, ("write:tasks",))
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
    }, actor=services.principal_actor(principal), project=project)
    return services.dumps(result)


def get_project_board(board_id: str, project: str = "maxwell") -> str:
    """Fetch one Board/Mission child by id from one Project."""
    services = _services()
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    result = store.get_project_board(board_id, project=project)
    return services.dumps(result or {"error": "unknown board", "board_id": board_id, "project": project})


def list_project_boards(project: str = "maxwell", kind: str = "",
                        status: str = "") -> str:
    """List Board/Mission children under one Project, optionally filtered by kind/status."""
    services = _services()
    if not store.has_project(project):
        return services.dumps({"error": f"unknown project: {project}", "project": project})
    return services.dumps({"project": project, "boards": store.list_project_boards(
        project=project, kind=kind, status=status)})


PROJECT_TOOL_NAMES = (
    "get_project", "update_project", "get_project_impact_report",
    "archive_project", "restore_project",
    "plan_project_consolidation", "apply_project_consolidation",
    "verify_project_consolidation", "rollback_project_consolidation",
    "create_project_purge_intent", "verify_project_purge_intent",
    "execute_project_purge", "record_project_cleanup_review",
    "create_project", "set_project_github_repo", "set_project_repo_topology",
    "get_project_execution_policy", "set_project_execution_policy",
    "create_project_board", "get_project_board", "list_project_boards",
)


def register_project_tools(mcp: Any,
                           services: ProjectToolServices) -> dict[str, Callable[..., str]]:
    global _SERVICES
    _SERVICES = services
    registered = {}
    for name in PROJECT_TOOL_NAMES:
        function = globals()[name]
        mcp.tool()(function)
        registered[name] = function
    return registered
