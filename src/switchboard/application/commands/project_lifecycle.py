"""Fail-closed project archive and restore application commands (ACCESS-20)."""
from __future__ import annotations

import copy
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from switchboard.application.queries import project_impact
from switchboard.contracts.projects import (
    ArchiveProjectCommand,
    PROJECT_IMPACT_REPORT_SCHEMA,
    ProjectImpactReceipt,
    RestoreProjectCommand,
    build_impact_receipt,
)
from switchboard.storage.repositories.project_impact import (
    ProjectImpactRepository,
    default_project_impact_repository,
)
from switchboard.storage.repositories.protocols.access import AccessRepository


TRANSITION_RESULT_SCHEMA = "switchboard.project_lifecycle_transition.v1"


def _invalid(command: str, exc: Exception) -> dict[str, Any]:
    return {
        "schema": TRANSITION_RESULT_SCHEMA,
        "error": f"invalid_{command}_project_command",
        "message": str(exc),
    }


def archive_project(
        payload: Mapping[str, Any], *,
        access_repository: AccessRepository,
        project_configs: Mapping[str, Mapping[str, Any]],
        registry_db_path: str,
        repo_topology_provider: Callable[[str], Mapping[str, Any]],
        impact_repository: ProjectImpactRepository = default_project_impact_repository,
        ) -> dict[str, Any]:
    """Archive only against an exact, current, archive-eligible impact receipt."""
    try:
        command = ArchiveProjectCommand.model_validate(dict(payload or {}))
        supplied_receipt = ProjectImpactReceipt.model_validate(
            command.impact_report_receipt).model_dump(by_alias=True)
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("archive", exc)

    project_id = command.project_id
    if (supplied_receipt.get("project_id") != project_id or
            supplied_receipt.get("report_schema") != PROJECT_IMPACT_REPORT_SCHEMA):
        return {
            "schema": TRANSITION_RESULT_SCHEMA,
            "error": "invalid_impact_report_receipt",
            "project_id": project_id,
            "receipt": supplied_receipt,
        }
    if not access_repository.has_project(project_id):
        return {"schema": TRANSITION_RESULT_SCHEMA,
                "error": f"unknown project: {project_id}"}
    current = access_repository.get_project_record(project_id)
    if current.get("error"):
        return current
    if current.get("lifecycle_status") == "archived":
        return {
            "schema": TRANSITION_RESULT_SCHEMA,
            "action": "archive",
            "project": current,
            "transitioned": False,
            "idempotent": True,
        }

    report = project_impact.execute_for(
        project_id,
        access_repository=access_repository,
        project_configs=project_configs,
        registry_db_path=registry_db_path,
        repo_topology_provider=repo_topology_provider,
        impact_repository=impact_repository,
    )
    if report.get("error"):
        return {"schema": TRANSITION_RESULT_SCHEMA,
                "error": "impact_report_unavailable", "impact_report": report}
    current_receipt = report.get("receipt") or {}
    if supplied_receipt != current_receipt:
        return {
            "schema": TRANSITION_RESULT_SCHEMA,
            "error": "stale_impact_report_receipt",
            "project_id": project_id,
            "supplied_receipt": supplied_receipt,
            "current_receipt": current_receipt,
        }
    archive_gate = ((report.get("recommendation") or {}).get("actions") or {}).get("archive") or {}
    if archive_gate.get("eligible") is not True:
        return {
            "schema": TRANSITION_RESULT_SCHEMA,
            "error": "project_archive_blocked",
            "project_id": project_id,
            "blocking_findings": report.get("blocking_findings") or [],
            "recommendation": report.get("recommendation") or {},
            "receipt": current_receipt,
        }

    transition = access_repository.transition_project_lifecycle(
        project_id, "archived", actor=command.actor, reason=command.reason,
        impact_report_hash=current_receipt.get("report_hash") or "",
        validation={
            "receipt": current_receipt,
            "archive_eligible": True,
            "finding_codes": [item.get("code") for item in report.get("blocking_findings") or []],
        },
    )
    if transition.get("error"):
        return transition
    # Close the report->transition race fail-closed. Every connection re-checks the
    # lifecycle state at SQL execution time, so writes after the transition are denied.
    # This post-transition snapshot catches a writer that committed in the narrow window
    # between the current receipt check and the registry transition; if that happened,
    # immediately restore the project instead of leaving an unreviewed archive in place.
    post_report = project_impact.execute_for(
        project_id,
        access_repository=access_repository,
        project_configs=project_configs,
        registry_db_path=registry_db_path,
        repo_topology_provider=repo_topology_provider,
        impact_repository=impact_repository,
    )
    normalized_post = copy.deepcopy(post_report)
    normalized_project = normalized_post.get("project") or {}
    for key in ("lifecycle_status", "archived_at", "archived_by", "archive_reason"):
        normalized_project[key] = current.get(key)
    normalized_post["project"] = normalized_project
    normalized_post["receipt"] = build_impact_receipt(normalized_post)
    if normalized_post.get("receipt") != current_receipt:
        rollback = access_repository.transition_project_lifecycle(
            project_id, "active", actor=command.actor,
            reason="automatic rollback: project changed during archive",
            validation={
                "archive_race_detected": True,
                "expected_receipt": current_receipt,
                "observed_receipt": normalized_post.get("receipt"),
            },
        )
        return {
            "schema": TRANSITION_RESULT_SCHEMA,
            "error": "project_changed_during_archive",
            "project_id": project_id,
            "expected_receipt": current_receipt,
            "observed_receipt": normalized_post.get("receipt"),
            "rollback": rollback,
        }
    return {"schema": TRANSITION_RESULT_SCHEMA, "action": "archive", **transition}


def restore_project(
        payload: Mapping[str, Any], *,
        access_repository: AccessRepository,
        repo_topology_provider: Callable[[str], Mapping[str, Any]],
        ) -> dict[str, Any]:
    """Restore writes only after the project access and canonical topology validate."""
    try:
        command = RestoreProjectCommand.model_validate(dict(payload or {}))
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("restore", exc)

    project_id = command.project_id
    if not access_repository.has_project(project_id):
        return {"schema": TRANSITION_RESULT_SCHEMA,
                "error": f"unknown project: {project_id}"}
    current = access_repository.get_project_record(project_id)
    if current.get("error"):
        return current
    if current.get("lifecycle_status") == "purged":
        return {
            "schema": TRANSITION_RESULT_SCHEMA,
            "error": "project_purged",
            "project_id": project_id,
            "message": "purged projects cannot be restored",
        }
    access = access_repository.project_access(project_id) or {}
    access_valid = bool(access.get("project_id") == project_id and access.get("org_id"))
    try:
        topology = dict(repo_topology_provider(project_id) or {})
    except Exception as exc:  # noqa: BLE001 - validation must fail closed
        topology = {"valid": False, "error": type(exc).__name__}
    # A project may intentionally have no code repository. Validation rejects malformed
    # topology, while the existing code_repo_gate continues to expose a missing canonical
    # repo for workflows that actually require merge provenance.
    topology_valid = bool(
        topology.get("schema") and not (topology.get("invalid") or [])
    )
    validation = {
        "access_valid": access_valid,
        "topology_valid": topology_valid,
        "org_id": access.get("org_id") or None,
        "code_repo_gate": topology.get("code_repo_gate") or None,
    }
    if not access_valid or not topology_valid:
        return {
            "schema": TRANSITION_RESULT_SCHEMA,
            "error": "project_restore_validation_failed",
            "project_id": project_id,
            "validation": validation,
        }

    transition = access_repository.transition_project_lifecycle(
        project_id, "active", actor=command.actor, reason=command.reason,
        validation=validation,
    )
    if transition.get("error"):
        return transition
    return {"schema": TRANSITION_RESULT_SCHEMA, "action": "restore", **transition}
