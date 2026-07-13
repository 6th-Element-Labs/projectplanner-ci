"""Receipt-gated plan/apply/verify/rollback project consolidation (ACCESS-23)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from switchboard.application.queries import project_impact
from switchboard.contracts.projects import (
    ApplyProjectConsolidationCommand,
    PROJECT_CONSOLIDATION_PLAN_SCHEMA,
    PlanProjectConsolidationCommand,
    RollbackProjectConsolidationCommand,
    build_consolidation_plan_receipt,
)
from switchboard.storage.repositories.project_consolidation import (
    ProjectConsolidationRepository,
    default_project_consolidation_repository,
)
from switchboard.storage.repositories.project_impact import (
    ProjectImpactRepository,
    default_project_impact_repository,
)
from switchboard.storage.repositories.protocols.access import AccessRepository


PLAN_RESULT_SCHEMA = PROJECT_CONSOLIDATION_PLAN_SCHEMA
APPLY_RESULT_SCHEMA = "switchboard.project_consolidation.apply.v1"
VERIFY_RESULT_SCHEMA = "switchboard.project_consolidation.verify.v1"
ROLLBACK_RESULT_SCHEMA = "switchboard.project_consolidation.rollback.v1"


def _invalid(action: str, exc: Exception) -> dict[str, Any]:
    return {
        "schema": f"switchboard.project_consolidation.{action}.v1",
        "error": f"invalid_project_consolidation_{action}",
        "message": str(exc),
    }


def _plan_id(body: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(body), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "project-consolidation-plan-" + hashlib.sha256(encoded).hexdigest()[:16]


def plan_project_consolidation(
        payload: Mapping[str, Any], *,
        access_repository: AccessRepository,
        project_configs: Mapping[str, Mapping[str, Any]],
        registry_db_path: str,
        repo_topology_provider: Callable[[str], Mapping[str, Any]],
        consolidation_repository: ProjectConsolidationRepository =
            default_project_consolidation_repository,
        impact_repository: ProjectImpactRepository = default_project_impact_repository,
        ) -> dict[str, Any]:
    """Build one deterministic dry-run plan; never mutate project or board state."""
    try:
        command = PlanProjectConsolidationCommand.model_validate(dict(payload or {}))
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("plan", exc)

    source_id = command.source_project_id
    target_id = command.replacement_project_id
    if not access_repository.has_project(source_id):
        return {"schema": PLAN_RESULT_SCHEMA, "error": f"unknown project: {source_id}"}
    if not access_repository.has_project(target_id):
        return {"schema": PLAN_RESULT_SCHEMA, "error": f"unknown project: {target_id}"}
    source = access_repository.get_project_record(source_id)
    target = access_repository.get_project_record(target_id)
    if source.get("is_protected"):
        return {"schema": PLAN_RESULT_SCHEMA, "error": "protected_project",
                "message": "protected projects cannot be consolidation sources"}
    if source.get("lifecycle_status") != "active":
        return {"schema": PLAN_RESULT_SCHEMA, "error": "source_project_not_active",
                "lifecycle_status": source.get("lifecycle_status")}
    if target.get("lifecycle_status") != "active":
        return {"schema": PLAN_RESULT_SCHEMA, "error": "replacement_project_not_active",
                "lifecycle_status": target.get("lifecycle_status")}

    report = project_impact.execute_for(
        source_id,
        access_repository=access_repository,
        project_configs=project_configs,
        registry_db_path=registry_db_path,
        repo_topology_provider=repo_topology_provider,
        impact_repository=impact_repository,
    )
    if report.get("error"):
        return {"schema": PLAN_RESULT_SCHEMA, "error": "impact_report_unavailable",
                "impact_report": report}
    consolidate_gate = ((report.get("recommendation") or {}).get("actions") or {}).get(
        "consolidate") or {}
    if consolidate_gate.get("eligible") is not True:
        return {
            "schema": PLAN_RESULT_SCHEMA,
            "error": "project_consolidation_blocked",
            "blocking_findings": report.get("blocking_findings") or [],
            "recommendation": report.get("recommendation") or {},
            "impact_receipt": report.get("receipt") or {},
        }

    target_snapshot = consolidation_repository.target_snapshot(
        target_id, project_configs,
        board_id=command.replacement_board_id or "",
        mission_id=command.replacement_mission_id or "",
        deliverable_id=command.replacement_deliverable_id or "",
    )
    if not target_snapshot.get("valid"):
        return {"schema": PLAN_RESULT_SCHEMA, "error": "invalid_replacement_target",
                "target": target_snapshot}
    history = consolidation_repository.history_snapshot(source_id, project_configs)
    if history.get("error"):
        return {"schema": PLAN_RESULT_SCHEMA, **history}
    routing = consolidation_repository.routing_plan(
        source_id, target_id, list(command.safe_routing_keys), project_configs)
    if routing.get("error"):
        return {"schema": PLAN_RESULT_SCHEMA, **routing}

    approval = command.approval.model_dump(by_alias=True)
    body = {
        "schema": PLAN_RESULT_SCHEMA,
        "source_project_id": source_id,
        "replacement_project_id": target_id,
        "replacement_board_id": command.replacement_board_id,
        "replacement_mission_id": command.replacement_mission_id,
        "replacement_deliverable_id": command.replacement_deliverable_id,
        "reason": command.reason,
        "planned_by": command.actor,
        "approval": approval,
        "impact_receipt": report.get("receipt") or {},
        "history": history,
        "target": target_snapshot,
        "inventory": {
            "tasks": report.get("tasks") or {},
            "provenance": report.get("provenance") or {},
            "hosted_outcomes": report.get("hosted_outcomes") or {},
            "cross_project_links": report.get("cross_project_links") or {},
            "repo_ci_webhooks": report.get("repo_ci_webhooks") or {},
            "access": report.get("access") or {},
            "communications": report.get("communications") or {},
            "automation": report.get("automation") or {},
        },
        "routing_rewrites": routing,
        "preservation_policy": {
            "copy_tasks": False,
            "rewrite_task_dependencies": False,
            "rewrite_deliverable_task_links": False,
            "fabricate_done_state": False,
            "source_history_remains_authoritative": True,
            "allowed_routing_keys": routing.get("keys") or [],
        },
        "apply_steps": [
            "revalidate exact plan and impact receipts",
            "archive source to freeze every write surface",
            "transfer only allowlisted routing values",
            "record replacement and consolidation pointers",
            "verify history fingerprint, pointers, routes, and archived graph reads",
        ],
    }
    body["plan_id"] = _plan_id(body)
    body["confirmation"] = f"CONSOLIDATE {source_id} INTO {target_id}"
    body["receipt"] = build_consolidation_plan_receipt(body)
    return body


def verify_project_consolidation(
        source_project_id: str, consolidation_id: str, *,
        access_repository: AccessRepository,
        project_configs: Mapping[str, Mapping[str, Any]],
        registry_db_path: str,
        repo_topology_provider: Callable[[str], Mapping[str, Any]],
        consolidation_repository: ProjectConsolidationRepository =
            default_project_consolidation_repository,
        impact_repository: ProjectImpactRepository = default_project_impact_repository,
        persist: bool = True,
        ) -> dict[str, Any]:
    source_id = str(source_project_id or "").strip()
    record = consolidation_repository.get(str(consolidation_id or "").strip())
    if not source_id or not record or record.get("source_project_id") != source_id:
        return {"schema": VERIFY_RESULT_SCHEMA,
                "error": "unknown_project_consolidation"}
    source = access_repository.get_project_record(source_id)
    target = access_repository.get_project_record(record.get("replacement_project_id") or "")
    current_history = consolidation_repository.history_snapshot(source_id, project_configs)
    routing = consolidation_repository.routing_matches(record, project_configs)
    report = project_impact.execute_for(
        source_id,
        access_repository=access_repository,
        project_configs=project_configs,
        registry_db_path=registry_db_path,
        repo_topology_provider=repo_topology_provider,
        impact_repository=impact_repository,
    )
    plan = record.get("plan") or {}
    checks = {
        "source_archived": source.get("lifecycle_status") == "archived",
        "replacement_active": target.get("lifecycle_status") == "active",
        "replacement_project_pointer": (
            source.get("replacement_project_id") == record.get("replacement_project_id")),
        "replacement_board_pointer": (
            source.get("replacement_board_id") == record.get("replacement_board_id")),
        "replacement_mission_pointer": (
            source.get("replacement_mission_id") == record.get("replacement_mission_id")),
        "replacement_deliverable_pointer": (
            source.get("replacement_deliverable_id") == record.get("replacement_deliverable_id")),
        "consolidation_pointer": (
            source.get("replacement_consolidation_id") == record.get("consolidation_id")),
        "source_history_preserved": (
            current_history.get("history_hash") ==
            (record.get("history") or {}).get("history_hash")),
        "safe_routing_rewrites_match": routing.get("ok") is True,
        "cross_project_graph_preserved": (
            (report.get("cross_project_links") or {}) ==
            ((plan.get("inventory") or {}).get("cross_project_links") or {})),
        "tasks_not_copied": bool((plan.get("preservation_policy") or {}).get(
            "copy_tasks") is False),
        "done_state_not_fabricated": bool((plan.get("preservation_policy") or {}).get(
            "fabricate_done_state") is False),
    }
    verified = all(checks.values())
    if verified and persist and record.get("status") != "verified":
        record = consolidation_repository.mark_verified(consolidation_id) or record
    return {
        "schema": VERIFY_RESULT_SCHEMA,
        "consolidation_id": consolidation_id,
        "source_project_id": source_id,
        "replacement_project_id": record.get("replacement_project_id"),
        "verified": verified,
        "checks": checks,
        "routing": routing,
        "record_status": record.get("status"),
    }


def apply_project_consolidation(
        payload: Mapping[str, Any], *,
        access_repository: AccessRepository,
        project_configs: Mapping[str, Mapping[str, Any]],
        registry_db_path: str,
        repo_topology_provider: Callable[[str], Mapping[str, Any]],
        consolidation_repository: ProjectConsolidationRepository =
            default_project_consolidation_repository,
        impact_repository: ProjectImpactRepository = default_project_impact_repository,
        ) -> dict[str, Any]:
    try:
        command = ApplyProjectConsolidationCommand.model_validate(dict(payload or {}))
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("apply", exc)
    plan = dict(command.plan)
    receipt = dict(plan.get("receipt") or {})
    plan_hash = str(receipt.get("plan_hash") or "")
    existing = consolidation_repository.get_by_plan_hash(plan_hash) if plan_hash else None
    if existing:
        if existing.get("status") in {"applied", "verified"}:
            return {"schema": APPLY_RESULT_SCHEMA, "applied": True, "idempotent": True,
                    "record": existing}
        return {"schema": APPLY_RESULT_SCHEMA, "error": "consolidation_was_rolled_back",
                "record": existing}
    if plan.get("schema") != PLAN_RESULT_SCHEMA or not receipt:
        return {"schema": APPLY_RESULT_SCHEMA, "error": "invalid_consolidation_plan"}
    if command.confirmation != plan.get("confirmation"):
        return {"schema": APPLY_RESULT_SCHEMA, "error": "confirmation_mismatch",
                "expected": plan.get("confirmation")}
    recomputed = plan_project_consolidation(
        {
            "source_project_id": plan.get("source_project_id"),
            "replacement_project_id": plan.get("replacement_project_id"),
            "replacement_board_id": plan.get("replacement_board_id"),
            "replacement_mission_id": plan.get("replacement_mission_id"),
            "replacement_deliverable_id": plan.get("replacement_deliverable_id"),
            "safe_routing_keys": (plan.get("routing_rewrites") or {}).get("keys") or [],
            "reason": plan.get("reason"),
            "actor": plan.get("planned_by"),
            "approval": plan.get("approval"),
        },
        access_repository=access_repository,
        project_configs=project_configs,
        registry_db_path=registry_db_path,
        repo_topology_provider=repo_topology_provider,
        consolidation_repository=consolidation_repository,
        impact_repository=impact_repository,
    )
    if recomputed.get("error") or recomputed != plan:
        return {"schema": APPLY_RESULT_SCHEMA, "error": "stale_consolidation_plan",
                "current_plan": recomputed}

    source_id = str(plan.get("source_project_id") or "")
    history = consolidation_repository.history_snapshot(source_id, project_configs)
    transition = access_repository.transition_project_lifecycle(
        source_id, "archived", actor=command.actor,
        reason=f"consolidation freeze: {plan.get('reason')}",
        impact_report_hash=(plan.get("impact_receipt") or {}).get("report_hash") or "",
        validation={
            "consolidation_plan_hash": plan_hash,
            "operator_approval": plan.get("approval") or {},
            "write_freeze": True,
            "preserve_source_history": True,
        },
    )
    if transition.get("error"):
        return {"schema": APPLY_RESULT_SCHEMA, "error": "consolidation_freeze_failed",
                "transition": transition}
    applied = consolidation_repository.apply(
        plan, history, actor=command.actor, project_configs=project_configs)
    if applied.get("error"):
        rollback = access_repository.transition_project_lifecycle(
            source_id, "active", actor=command.actor,
            reason="automatic rollback: consolidation apply failed",
            validation={"consolidation_plan_hash": plan_hash, "apply_error": applied},
        )
        return {"schema": APPLY_RESULT_SCHEMA, **applied, "rollback": rollback}
    record = applied.get("record") or {}
    verification = verify_project_consolidation(
        source_id, record.get("consolidation_id") or "",
        access_repository=access_repository,
        project_configs=project_configs,
        registry_db_path=registry_db_path,
        repo_topology_provider=repo_topology_provider,
        consolidation_repository=consolidation_repository,
        impact_repository=impact_repository,
    )
    if not verification.get("verified"):
        repository_rollback = consolidation_repository.rollback(
            record, actor=command.actor,
            reason="automatic rollback: consolidation verification failed",
            project_configs=project_configs,
        )
        lifecycle_rollback = access_repository.transition_project_lifecycle(
            source_id, "active", actor=command.actor,
            reason="automatic rollback: consolidation verification failed",
            validation={"verification": verification},
        )
        return {"schema": APPLY_RESULT_SCHEMA,
                "error": "consolidation_verification_failed",
                "verification": verification,
                "rollback": {"repository": repository_rollback,
                             "lifecycle": lifecycle_rollback}}
    return {
        "schema": APPLY_RESULT_SCHEMA,
        "applied": True,
        "idempotent": bool(applied.get("idempotent")),
        "record": consolidation_repository.get(record.get("consolidation_id") or ""),
        "verification": verification,
    }


def rollback_project_consolidation(
        payload: Mapping[str, Any], *,
        access_repository: AccessRepository,
        project_configs: Mapping[str, Mapping[str, Any]],
        consolidation_repository: ProjectConsolidationRepository =
            default_project_consolidation_repository,
        ) -> dict[str, Any]:
    try:
        command = RollbackProjectConsolidationCommand.model_validate(dict(payload or {}))
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("rollback", exc)
    record = consolidation_repository.get(command.consolidation_id)
    if not record or record.get("source_project_id") != command.source_project_id:
        return {"schema": ROLLBACK_RESULT_SCHEMA,
                "error": "unknown_project_consolidation"}
    repository_rollback = consolidation_repository.rollback(
        record, actor=command.actor, reason=command.reason,
        project_configs=project_configs,
    )
    if repository_rollback.get("error"):
        return {"schema": ROLLBACK_RESULT_SCHEMA, **repository_rollback}
    current = access_repository.get_project_record(command.source_project_id)
    if current.get("lifecycle_status") == "archived":
        lifecycle = access_repository.transition_project_lifecycle(
            command.source_project_id, "active", actor=command.actor,
            reason=f"consolidation rollback: {command.reason}",
            validation={
                "consolidation_id": command.consolidation_id,
                "rollback_receipt": (record.get("rollback") or {}),
                "before_purge": True,
            },
        )
    else:
        lifecycle = {"transitioned": False, "idempotent": True,
                     "project": current}
    if lifecycle.get("error"):
        return {"schema": ROLLBACK_RESULT_SCHEMA,
                "error": "consolidation_lifecycle_restore_failed",
                "repository_rollback": repository_rollback,
                "lifecycle": lifecycle}
    result = {
        "schema": ROLLBACK_RESULT_SCHEMA,
        "rolled_back": True,
        "idempotent": bool(repository_rollback.get("idempotent")
                           and lifecycle.get("idempotent")),
        "consolidation_id": command.consolidation_id,
        "source_project_id": command.source_project_id,
        "project": lifecycle.get("project"),
        "rollback_receipt": record.get("rollback") or {},
    }
    receipt_body = dict(result)
    result["receipt_hash"] = "sha256:" + hashlib.sha256(json.dumps(
        receipt_body, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()
    return result
