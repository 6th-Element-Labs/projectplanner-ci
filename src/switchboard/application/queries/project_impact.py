"""Application query for a deterministic project lifecycle impact report."""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Mapping

from switchboard.contracts.projects import ProjectImpactReport, build_impact_receipt
from switchboard.storage.repositories.project_impact import (
    ProjectImpactRepository,
    default_project_impact_repository,
)
from switchboard.storage.repositories.protocols.access import AccessRepository


MAX_SAMPLE_LIMIT = 200
DEFAULT_SAMPLE_LIMIT = 50


def _finding(code: str, severity: str, message: str, count: int = 1,
             category: str = "operational") -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "category": category,
        "blocking": True,
        "count": int(count),
        "message": message,
    }


def _repo_summary(topology: Mapping[str, Any]) -> dict[str, Any]:
    roles = {}
    for role_name, role in sorted((topology.get("roles") or {}).items()):
        roles[role_name] = {
            "configured": bool(role.get("configured") or role.get("repo")),
            "repo": role.get("repo") or None,
            "default_branch": role.get("default_branch") or None,
            "required_status_contexts": sorted(role.get("required_status_contexts") or []),
            "authority": sorted(role.get("authority") or []),
        }
    return {
        "schema": topology.get("schema"),
        "topology_type": topology.get("topology_type"),
        "valid": bool(topology.get("valid")),
        "warnings": sorted(topology.get("warnings") or []),
        "invalid": sorted(topology.get("invalid") or []),
        "missing": sorted(topology.get("missing") or []),
        "roles": roles,
        "code_repo_gate": topology.get("code_repo_gate") or {},
    }


def _findings(project: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    tasks = snapshot["tasks"]
    provenance = snapshot["provenance"]
    coordination = snapshot["coordination"]
    hosted = snapshot["hosted_outcomes"]
    links = snapshot["cross_project_links"]
    repo = snapshot["repo_ci_webhooks"]
    access = snapshot["access"]
    comms = snapshot["communications"]
    automation = snapshot["automation"]

    incomplete_surfaces = []
    database_read = snapshot["storage"].get("database_read") or {}
    if not database_read.get("available"):
        incomplete_surfaces.append({"surface": "project_database", **database_read})
    registry_read = access.get("registry_read") or {}
    if not registry_read.get("available"):
        incomplete_surfaces.append({"surface": "project_registry", **registry_read})
    graph_scan = links.get("scan") or {}
    if not graph_scan.get("complete"):
        incomplete_surfaces.append({
            "surface": "cross_project_graph",
            "error_code": "project_database_unavailable",
            "unavailable_project_count": graph_scan.get("unavailable_project_count") or 0,
        })
    topology_read = (repo.get("topology") or {}).get("read") or {}
    if not topology_read.get("available"):
        incomplete_surfaces.append({"surface": "repo_topology", **topology_read})
    if incomplete_surfaces:
        finding = _finding(
            "snapshot_incomplete", "critical",
            "Project impact snapshot is incomplete; archive eligibility cannot be established.",
            len(incomplete_surfaces), category="protection")
        finding["affected_surfaces"] = incomplete_surfaces
        out.append(finding)

    if project.get("is_protected"):
        out.append(_finding("protected_project", "critical",
                            "Protected projects cannot be archived.", category="protection"))
    if tasks["nonterminal"]["total"]:
        out.append(_finding("nonterminal_work", "high",
                            "Project has nonterminal tasks.", tasks["nonterminal"]["total"]))
    if provenance["open_prs"]["total"]:
        out.append(_finding("open_pr_provenance", "high",
                            "Project has pushed PR provenance without terminal merge proof.",
                            provenance["open_prs"]["total"]))
    if coordination["active_claims"]["total"]:
        out.append(_finding("active_claims", "high", "Project has active task claims.",
                            coordination["active_claims"]["total"]))
    if coordination["active_work_sessions"]["total"]:
        out.append(_finding("active_work_sessions", "high", "Project has active Work Sessions.",
                            coordination["active_work_sessions"]["total"]))
    hosted_count = hosted["active_board_count"] + hosted["active_deliverable_count"]
    if hosted_count:
        out.append(_finding("hosted_outcomes", "medium",
                            "Project hosts active boards, missions, or deliverables.", hosted_count,
                            category="structural"))
    if links["total"]:
        out.append(_finding("cross_project_links", "high",
                            "Project participates in inbound or outbound cross-project links.",
                            links["total"], category="structural"))
    configured_roles = [name for name, role in (repo.get("topology") or {}).get("roles", {}).items()
                        if role.get("configured")]
    if configured_roles:
        out.append(_finding("repo_integrations_configured", "medium",
                            "Project has configured repository integration roles.",
                            len(configured_roles), category="structural"))
    pending_repo = repo["pending_external_ci_count"] + repo["pending_webhook_count"]
    if pending_repo:
        out.append(_finding("pending_ci_or_webhooks", "high",
                            "Project has pending or failed CI/webhook work.", pending_repo))
    if access["active_token_principal_count"]:
        out.append(_finding("active_credentials", "high",
                            "Project has active token principals that require revocation or transfer.",
                            access["active_token_principal_count"]))
    pending_comms = comms["pending_inbox_count"] + comms["unacked_agent_message_count"]
    if pending_comms:
        out.append(_finding("pending_communications", "high",
                            "Project has pending inbox or unacknowledged agent communications.",
                            pending_comms))
    routing_count = comms["inbound_domains_total"]
    if routing_count or comms["digest_recipient_count"] or comms["notify_recipient_count"]:
        out.append(_finding("communications_routing_configured", "medium",
                            "Project has inbound or outbound communications routing to transfer.",
                            routing_count + comms["digest_recipient_count"] +
                            comms["notify_recipient_count"], category="structural"))
    active_automation = automation["active_background_job_count"] + automation["active_monitor_count"]
    if active_automation:
        out.append(_finding("active_automation", "high",
                            "Project has active background jobs or coordination monitors.",
                            active_automation))
    return sorted(out, key=lambda item: item["code"])


def _recommendation(findings: list[dict[str, Any]]) -> dict[str, Any]:
    operational = [item["code"] for item in findings if item["category"] != "structural"]
    structural = [item["code"] for item in findings if item["category"] == "structural"]
    protected = "protected_project" in operational
    if operational:
        action = "keep"
        reasons = operational + structural
    elif structural:
        action = "consolidate"
        reasons = structural
    else:
        action = "archive"
        reasons = ["no_blocking_findings"]
    return {
        "action": action,
        "reasons": reasons,
        "actions": {
            "keep": {
                "recommended": action == "keep",
                "eligible": True,
                "reasons": operational + structural if action == "keep" else [],
            },
            "consolidate": {
                "recommended": action == "consolidate",
                "eligible": not protected and not operational,
                "reasons": structural,
            },
            "archive": {
                "recommended": action == "archive",
                "eligible": not findings,
                "reasons": [] if not findings else [item["code"] for item in findings],
            },
        },
    }


def execute_for(project: str, *, access_repository: AccessRepository,
                project_configs: Mapping[str, Mapping[str, Any]],
                registry_db_path: str,
                repo_topology_provider: Callable[[str], Mapping[str, Any]],
                limit: int = DEFAULT_SAMPLE_LIMIT,
                impact_repository: ProjectImpactRepository = default_project_impact_repository,
                ) -> dict[str, Any]:
    """Build the shared service/MCP/REST read model for one accessible project."""
    project_id = str(project or "").strip()
    if not project_id or not access_repository.has_project(project_id):
        return {"error": f"unknown project: {project_id}"}
    bounded_limit = max(1, min(MAX_SAMPLE_LIMIT, int(limit or DEFAULT_SAMPLE_LIMIT)))
    project_record = access_repository.get_project_record(project_id)
    if project_record.get("error"):
        return project_record
    snapshot = impact_repository.collect(
        project_id,
        project_configs=project_configs,
        registry_db_path=registry_db_path,
        limit=bounded_limit,
    )
    try:
        topology = _repo_summary(repo_topology_provider(project_id) or {})
        topology["read"] = {"available": True, "error_code": None, "error_type": None}
    except (OSError, sqlite3.Error) as exc:
        topology = _repo_summary({})
        topology["read"] = {
            "available": False,
            "error_code": "repo_topology_unavailable",
            "error_type": type(exc).__name__,
        }
    snapshot["repo_ci_webhooks"]["topology"] = topology
    safe_project = {key: project_record.get(key) for key in (
        "id", "label", "pretitle", "org_id", "owner_user_id", "purpose", "boundary",
        "visibility", "lifecycle_status", "archived_at", "archived_by", "archive_reason",
        "is_protected", "is_system", "is_builtin", "replacement_project_id",
        "replacement_board_id", "replacement_mission_id", "replacement_deliverable_id",
        "replacement_consolidation_id",
    )}
    findings = _findings(safe_project, snapshot)
    report_payload = {
        "project_id": project_id,
        "project": safe_project,
        "bounds": {
            "sample_limit": bounded_limit,
            "maximum_sample_limit": MAX_SAMPLE_LIMIT,
            "ordering": "stable_lexicographic",
            "time_basis": "persisted_state_only",
        },
        "blocking_findings": findings,
        "recommendation": _recommendation(findings),
        **snapshot,
    }
    report_payload["receipt"] = build_impact_receipt(report_payload)
    report = ProjectImpactReport(**report_payload)
    return report.model_dump(by_alias=True)
