"""Privileged, receipt-gated project purge workflow (ACCESS-24)."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from switchboard.application.queries import project_impact
from switchboard.contracts.projects import (
    CreatePurgeIntentCommand,
    ExecutePurgeCommand,
    ProjectImpactReceipt,
    RecordCleanupReviewCommand,
    VerifyPurgeIntentCommand,
)
from switchboard.storage.repositories.project_purge import (
    ProjectPurgeRepository,
    default_project_purge_repository,
)
from switchboard.storage.repositories.protocols.access import AccessRepository


PURGE_RESULT_SCHEMA = "switchboard.project_purge.result.v1"
CLEANUP_REVIEW_SCHEMA = "switchboard.project_cleanup.review.v1"


def _sha(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _invalid(action: str, exc: Exception) -> dict[str, Any]:
    return {"schema": PURGE_RESULT_SCHEMA, "error": f"invalid_project_purge_{action}",
            "message": str(exc)}


def _remove_project_database(db_path: str) -> bool:
    """Remove the isolated SQLite files, returning false when cleanup must be retried."""
    removed = True
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(db_path + suffix).unlink(missing_ok=True)
        except OSError:
            removed = False
    return removed


def _current_report(project_id: str, *, access_repository: AccessRepository,
                    project_configs: Mapping[str, Mapping[str, Any]], registry_db_path: str,
                    repo_topology_provider: Callable[[str], Mapping[str, Any]]) -> dict[str, Any]:
    return project_impact.execute_for(
        project_id, access_repository=access_repository, project_configs=project_configs,
        registry_db_path=registry_db_path, repo_topology_provider=repo_topology_provider)


def _gate(project_id: str, *, retention_days: int, export_created_at: float,
          immutable: bool, access_repository: AccessRepository,
          project_configs: Mapping[str, Mapping[str, Any]], registry_db_path: str,
          repo_topology_provider: Callable[[str], Mapping[str, Any]],
          now: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    record = access_repository.get_project_record(project_id)
    blockers: list[dict[str, Any]] = []
    if record.get("error"):
        return {}, [{"code": "unknown_project", "detail": record}]
    if record.get("lifecycle_status") != "archived":
        blockers.append({"code": "project_not_archived"})
    if record.get("is_protected") or record.get("is_system"):
        blockers.append({"code": "protected_project"})
    archived_at = float(record.get("archived_at") or 0)
    if not archived_at or now < archived_at + retention_days * 86400:
        blockers.append({"code": "archive_retention_not_met",
                         "eligible_at": archived_at + retention_days * 86400})
    if not immutable:
        blockers.append({"code": "export_not_immutable"})
    if export_created_at < archived_at:
        blockers.append({"code": "export_predates_archive"})
    if export_created_at > now:
        blockers.append({"code": "export_timestamp_in_future"})
    report = _current_report(
        project_id, access_repository=access_repository, project_configs=project_configs,
        registry_db_path=registry_db_path, repo_topology_provider=repo_topology_provider)
    if report.get("error"):
        blockers.append({"code": "impact_report_unavailable", "detail": report})
    for finding in report.get("blocking_findings") or []:
        blockers.append({"code": finding.get("code"), "detail": finding})
    # These explicit checks keep the destructive gate clear even if impact
    # recommendation policy changes independently.
    explicit = {
        "active_credentials": ((report.get("access") or {}).get("active_token_principal_count") or 0),
        "active_work_sessions": (((report.get("coordination") or {}).get("active_work_sessions") or {}).get("total") or 0),
        "active_claims": (((report.get("coordination") or {}).get("active_claims") or {}).get("total") or 0),
        "cross_project_links": ((report.get("cross_project_links") or {}).get("total") or 0),
    }
    existing = {item["code"] for item in blockers}
    blockers.extend({"code": code, "count": count} for code, count in explicit.items()
                    if count and code not in existing)
    return report, blockers


def create_purge_intent(payload: Mapping[str, Any], *, access_repository: AccessRepository,
                        project_configs: Mapping[str, Mapping[str, Any]], registry_db_path: str,
                        repo_topology_provider: Callable[[str], Mapping[str, Any]],
                        repository: ProjectPurgeRepository = default_project_purge_repository,
                        now_provider: Callable[[], float] = time.time) -> dict[str, Any]:
    try:
        command = CreatePurgeIntentCommand.model_validate(dict(payload or {}))
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("intent", exc)
    if command.typed_confirmation != f"PURGE {command.project_id}":
        return {"schema": PURGE_RESULT_SCHEMA, "error": "confirmation_mismatch",
                "expected": f"PURGE {command.project_id}"}
    now = now_provider()
    report, blockers = _gate(
        command.project_id, retention_days=command.retention_days,
        export_created_at=command.export.created_at, immutable=command.export.immutable,
        access_repository=access_repository, project_configs=project_configs,
        registry_db_path=registry_db_path, repo_topology_provider=repo_topology_provider,
        now=now)
    if blockers:
        return {"schema": PURGE_RESULT_SCHEMA, "error": "project_purge_blocked",
                "project_id": command.project_id, "blocking_findings": blockers,
                "impact_report": report}
    body = {
        "schema": "switchboard.project_purge.intent.v1", "project_id": command.project_id,
        "reason": command.reason, "actor": command.actor,
        "retention_days": command.retention_days,
        "export": command.export.model_dump(by_alias=True),
        "impact_report_receipt": report["receipt"],
    }
    intent_hash = _sha(body)
    intent_id = "purge-" + intent_hash.split(":", 1)[1][:20]
    record = repository.put_intent(registry_db_path, {
        "intent_id": intent_id, "project_id": command.project_id,
        "intent_hash": intent_hash, "impact_report_hash": report["receipt"]["report_hash"],
        "export_uri": command.export.artifact_uri, "export_hash": command.export.artifact_hash,
        "export_created_at": command.export.created_at, "retention_days": command.retention_days,
        "intent": body, "actor": command.actor, "reason": command.reason, "created_at": now,
    })
    return {"schema": PURGE_RESULT_SCHEMA, "action": "prepare", "record": record,
            "confirmation": f"VERIFY PURGE {command.project_id} {intent_id}",
            "mutated_project": False}


def verify_purge_intent(payload: Mapping[str, Any], *, access_repository: AccessRepository,
                        project_configs: Mapping[str, Mapping[str, Any]], registry_db_path: str,
                        repo_topology_provider: Callable[[str], Mapping[str, Any]],
                        repository: ProjectPurgeRepository = default_project_purge_repository,
                        now_provider: Callable[[], float] = time.time) -> dict[str, Any]:
    try:
        command = VerifyPurgeIntentCommand.model_validate(dict(payload or {}))
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("verify", exc)
    record = repository.get_intent(registry_db_path, command.intent_id)
    if not record or record.get("project_id") != command.project_id:
        return {"schema": PURGE_RESULT_SCHEMA, "error": "unknown_project_purge_intent"}
    expected = f"VERIFY PURGE {command.project_id} {command.intent_id}"
    if command.typed_confirmation != expected:
        return {"schema": PURGE_RESULT_SCHEMA, "error": "confirmation_mismatch", "expected": expected}
    if record.get("status") == "executed":
        return {"schema": PURGE_RESULT_SCHEMA, "action": "verify", "verified": True,
                "idempotent": True, "record": record}
    now = now_provider()
    report, blockers = _gate(
        command.project_id, retention_days=int(record["retention_days"]),
        export_created_at=float(record["export_created_at"]), immutable=True,
        access_repository=access_repository, project_configs=project_configs,
        registry_db_path=registry_db_path, repo_topology_provider=repo_topology_provider,
        now=now)
    if blockers or (report.get("receipt") or {}).get("report_hash") != record["impact_report_hash"]:
        return {"schema": PURGE_RESULT_SCHEMA, "error": "stale_or_blocked_project_purge",
                "blocking_findings": blockers, "current_receipt": report.get("receipt")}
    verified = repository.mark_verified(registry_db_path, command.intent_id,
                                          command.verifier, now)
    return {"schema": PURGE_RESULT_SCHEMA, "action": "verify", "verified": True,
            "record": verified,
            "execution_authorization": f"EXECUTE PURGE {command.project_id} {command.intent_id}"}


def execute_purge(payload: Mapping[str, Any], *, access_repository: AccessRepository,
                  project_configs: Mapping[str, Mapping[str, Any]], registry_db_path: str,
                  repo_topology_provider: Callable[[str], Mapping[str, Any]],
                  repository: ProjectPurgeRepository = default_project_purge_repository,
                  now_provider: Callable[[], float] = time.time) -> dict[str, Any]:
    try:
        command = ExecutePurgeCommand.model_validate(dict(payload or {}))
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("execute", exc)
    expected = f"EXECUTE PURGE {command.project_id} {command.intent_id}"
    if command.explicit_authorization != expected:
        return {"schema": PURGE_RESULT_SCHEMA, "error": "explicit_authorization_required",
                "expected": expected}
    intent = repository.get_intent(registry_db_path, command.intent_id)
    if not intent or intent.get("project_id") != command.project_id:
        return {"schema": PURGE_RESULT_SCHEMA, "error": "unknown_project_purge_intent"}
    db_path = str((project_configs.get(command.project_id) or {}).get("db") or "")
    if intent.get("status") == "executed":
        removed = _remove_project_database(db_path)
        intent = repository.mark_executed(
            registry_db_path, command.intent_id, command.actor,
            float(intent.get("executed_at") or now_provider()),
            database_removed=removed)
        return {"schema": PURGE_RESULT_SCHEMA, "action": "execute", "purged": removed,
                "idempotent": True, "database_removed": removed, "record": intent}
    if intent.get("status") != "verified":
        return {"schema": PURGE_RESULT_SCHEMA, "error": "project_purge_not_verified"}
    now = now_provider()
    report, blockers = _gate(
        command.project_id, retention_days=int(intent["retention_days"]),
        export_created_at=float(intent["export_created_at"]), immutable=True,
        access_repository=access_repository, project_configs=project_configs,
        registry_db_path=registry_db_path, repo_topology_provider=repo_topology_provider,
        now=now)
    if blockers or (report.get("receipt") or {}).get("report_hash") != intent["impact_report_hash"]:
        return {"schema": PURGE_RESULT_SCHEMA, "error": "stale_or_blocked_project_purge",
                "blocking_findings": blockers, "current_receipt": report.get("receipt")}
    registry_record = access_repository.get_project_record(command.project_id)
    audit_receipt = {
        "schema": "switchboard.project_purge.audit_receipt.v1",
        "intent_id": command.intent_id, "project_id": command.project_id,
        "intent_hash": intent["intent_hash"], "impact_report_hash": intent["impact_report_hash"],
        "export_hash": intent["export_hash"], "executed_by": command.actor, "executed_at": now,
    }
    audit_receipt["receipt_hash"] = _sha(audit_receipt)
    tombstone = repository.prepare_tombstone(
        registry_db_path, intent_id=command.intent_id, project_id=command.project_id,
        registry_record=registry_record, receipt=audit_receipt,
        database_path_hash=_sha({"path": os.path.abspath(db_path)}), created_at=now)
    transition = access_repository.transition_project_lifecycle(
        command.project_id, "purged", actor=command.actor,
        reason=f"guarded purge: {intent['reason']}", impact_report_hash=intent["impact_report_hash"],
        validation={"purge_intent_id": command.intent_id,
                    "purge_audit_receipt": audit_receipt,
                    "tombstone_id": tombstone.get("tombstone_id")})
    if transition.get("error"):
        return {"schema": PURGE_RESULT_SCHEMA, "error": "purge_tombstone_transition_failed",
                "transition": transition, "tombstone": tombstone}
    removed = _remove_project_database(db_path)
    executed = repository.mark_executed(
        registry_db_path, command.intent_id, command.actor, now, database_removed=removed)
    return {"schema": PURGE_RESULT_SCHEMA, "action": "execute", "purged": removed,
            "record": executed, "project": access_repository.get_project_record(command.project_id),
            "tombstone": {"tombstone_id": tombstone.get("tombstone_id"),
                          "database_removed": removed},
            "audit_receipt": audit_receipt}


def record_cleanup_review(payload: Mapping[str, Any], *, access_repository: AccessRepository,
                          project_configs: Mapping[str, Mapping[str, Any]], registry_db_path: str,
                          repo_topology_provider: Callable[[str], Mapping[str, Any]],
                          repository: ProjectPurgeRepository = default_project_purge_repository,
                          now_provider: Callable[[], float] = time.time) -> dict[str, Any]:
    try:
        command = RecordCleanupReviewCommand.model_validate(dict(payload or {}))
        supplied = ProjectImpactReceipt.model_validate(
            command.impact_report_receipt).model_dump(by_alias=True)
    except (ValidationError, ValueError, TypeError) as exc:
        return _invalid("cleanup_review", exc)
    report = _current_report(
        command.project_id, access_repository=access_repository, project_configs=project_configs,
        registry_db_path=registry_db_path, repo_topology_provider=repo_topology_provider)
    if report.get("receipt") != supplied:
        return {"schema": CLEANUP_REVIEW_SCHEMA, "error": "stale_impact_report_receipt",
                "current_receipt": report.get("receipt")}
    recommended = (report.get("recommendation") or {}).get("action")
    if command.decision != recommended:
        return {"schema": CLEANUP_REVIEW_SCHEMA, "error": "decision_requires_current_recommendation",
                "recommended": recommended}
    review_id = "cleanup-" + _sha({"project": command.project_id,
                                    "receipt": supplied}).split(":", 1)[1][:20]
    record = repository.record_cleanup_review(registry_db_path, {
        "review_id": review_id, "project_id": command.project_id,
        "decision": command.decision, "impact_report_hash": supplied["report_hash"],
        "impact_report_receipt": supplied, "approved_by": command.approved_by,
        "approved_at": command.approved_at, "rationale": command.rationale,
        "created_at": now_provider(),
    })
    return {"schema": CLEANUP_REVIEW_SCHEMA, "recorded": True, "mutated_project": False,
            "record": record}
