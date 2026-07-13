#!/usr/bin/env python3
"""ACCESS-24: guarded purge, tombstones, and durable cleanup reviews."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401 - adds repository src/ to sys.path


TMP = tempfile.mkdtemp(prefix="access24-purge-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_TOP_LEVEL_PROJECTS"] = "maxwell,helm,switchboard"

from fastapi.testclient import TestClient  # noqa: E402

import db.connection as db_connection  # noqa: E402
import store  # noqa: E402
from app import _write_required_scopes, app  # noqa: E402
import mcp_server  # noqa: E402
from switchboard.application.commands import project_lifecycle, project_purge  # noqa: E402
from switchboard.application.queries import project_impact  # noqa: E402
from switchboard.contracts import (  # noqa: E402
    CLEANUP_REVIEW_COMMAND_SCHEMA,
    PURGE_EXECUTE_COMMAND_SCHEMA,
    PURGE_INTENT_COMMAND_SCHEMA,
    PURGE_VERIFY_COMMAND_SCHEMA,
    get_schema,
)


NOW = 1_783_980_000.0
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def create_project(project_id: str) -> None:
    result = store.create_project(
        project_id, project_id=project_id, actor="access24-test",
        purpose="purge fixture", boundary="isolated fixture")
    assert result.get("created") is True, result
    store.init_db(project_id)


def impact(project_id: str) -> dict:
    return project_impact.execute_for(
        project_id, access_repository=store.access_repository,
        project_configs=store._project_map(), registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology)


def archive_and_age(project_id: str, days: int = 31) -> None:
    report = impact(project_id)
    result = project_lifecycle.archive_project(
        {"project_id": project_id, "reason": "retention fixture",
         "impact_report_receipt": report["receipt"], "actor": "access24-test"},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology)
    assert result.get("transitioned"), result
    with sqlite3.connect(store.PROJECT_REGISTRY_DB_PATH) as c:
        c.execute("UPDATE projects SET archived_at=?, updated_at=? WHERE id=?",
                  (NOW - days * 86400, NOW - days * 86400, project_id))
    db_connection.bust_project_cache()


def evidence(created_at=NOW - 1):
    return {"schema": "switchboard.project_purge.export_evidence.v1",
            "artifact_uri": "s3://immutable-backups/purge-fixture.tar.zst",
            "artifact_hash": "sha256:" + "a" * 64,
            "created_at": created_at, "immutable": True}


def prepare(project_id: str, **overrides) -> dict:
    payload = {"project_id": project_id, "reason": "retention elapsed",
               "actor": "operator/access24", "retention_days": 30,
               "typed_confirmation": f"PURGE {project_id}", "export": evidence()}
    payload.update(overrides)
    return project_purge.create_purge_intent(
        payload, access_repository=store.access_repository,
        project_configs=store._project_map(), registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        now_provider=lambda: NOW)


try:
    store.init_project_registry()
    for builtin in ("maxwell", "helm", "switchboard"):
        store.init_db(builtin)

    ok(all(get_schema(schema) for schema in (
        PURGE_INTENT_COMMAND_SCHEMA, PURGE_VERIFY_COMMAND_SCHEMA,
        PURGE_EXECUTE_COMMAND_SCHEMA, CLEANUP_REVIEW_COMMAND_SCHEMA)),
       "purge intent, verification, execution, and cleanup-review schemas are registered")

    create_project("too-young")
    archive_and_age("too-young", days=2)
    young = prepare("too-young")
    ok(young.get("error") == "project_purge_blocked"
       and "archive_retention_not_met" in {x["code"] for x in young["blocking_findings"]},
       "archived retention age is a fail-closed prerequisite")

    create_project("purge-fixture")
    archive_and_age("purge-fixture")
    wrong = prepare("purge-fixture", typed_confirmation="delete it")
    mutable = prepare("purge-fixture", export={**evidence(), "immutable": False})
    ok(wrong.get("error") == "confirmation_mismatch"
       and mutable.get("error") == "project_purge_blocked",
       "typed confirmation and immutable export evidence are mandatory")

    first = prepare("purge-fixture")
    repeated = prepare("purge-fixture")
    intent = first.get("record") or {}
    intent_id = intent.get("intent_id")
    ok(first.get("mutated_project") is False and first == repeated
       and intent.get("status") == "prepared",
       "intent preparation is deterministic, idempotent, and non-destructive")
    premature = project_purge.execute_purge(
        {"project_id": "purge-fixture", "intent_id": intent_id,
         "actor": "operator/access24",
         "explicit_authorization": f"EXECUTE PURGE purge-fixture {intent_id}"},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        now_provider=lambda: NOW)
    ok(premature.get("error") == "project_purge_not_verified",
       "execution cannot bypass the second verification step")

    verified = project_purge.verify_purge_intent(
        {"project_id": "purge-fixture", "intent_id": intent_id,
         "verifier": "operator/access24-reviewer",
         "typed_confirmation": f"VERIFY PURGE purge-fixture {intent_id}"},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        now_provider=lambda: NOW)
    ok(verified.get("verified") is True
       and verified.get("record", {}).get("verified_by") == "operator/access24-reviewer",
       "second verification recomputes gates and records its verifier")

    db_path = Path(store._project_map()["purge-fixture"]["db"])
    denied = project_purge.execute_purge(
        {"project_id": "purge-fixture", "intent_id": intent_id,
         "actor": "operator/access24", "explicit_authorization": "yes"},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        now_provider=lambda: NOW)
    ok(denied.get("error") == "explicit_authorization_required" and db_path.exists(),
       "production removal needs a separate exact execution authorization")

    executed = project_purge.execute_purge(
        {"project_id": "purge-fixture", "intent_id": intent_id,
         "actor": "operator/access24",
         "explicit_authorization": f"EXECUTE PURGE purge-fixture {intent_id}"},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        now_provider=lambda: NOW)
    ok(executed.get("purged") is True and not db_path.exists()
       and executed.get("project", {}).get("lifecycle_status") == "purged",
       "authorized execution removes only the isolated database and retains registry routing")
    ok(executed.get("tombstone", {}).get("database_removed") is True
       and str(executed.get("audit_receipt", {}).get("receipt_hash") or "").startswith("sha256:"),
       "purge retains a content-addressed tombstone and audit receipt")
    restored = project_lifecycle.restore_project(
        {"project_id": "purge-fixture", "reason": "must fail", "actor": "operator"},
        access_repository=store.access_repository,
        repo_topology_provider=store.get_project_repo_topology)
    ok(restored.get("error") == "project_purged",
       "restore remains possible before purge but is forbidden after purge")

    create_project("retry-cleanup")
    archive_and_age("retry-cleanup")
    retry_intent = prepare("retry-cleanup")["record"]["intent_id"]
    project_purge.verify_purge_intent(
        {"project_id": "retry-cleanup", "intent_id": retry_intent,
         "verifier": "operator/access24-reviewer",
         "typed_confirmation": f"VERIFY PURGE retry-cleanup {retry_intent}"},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        now_provider=lambda: NOW)
    retry_db = Path(store._project_map()["retry-cleanup"]["db"])
    original_unlink = Path.unlink

    def deny_database_once(path, *args, **kwargs):
        if path == retry_db:
            raise PermissionError("simulated removal failure")
        return original_unlink(path, *args, **kwargs)

    Path.unlink = deny_database_once
    try:
        incomplete = project_purge.execute_purge(
            {"project_id": "retry-cleanup", "intent_id": retry_intent,
             "actor": "operator/access24",
             "explicit_authorization": f"EXECUTE PURGE retry-cleanup {retry_intent}"},
            access_repository=store.access_repository, project_configs=store._project_map(),
            registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
            repo_topology_provider=store.get_project_repo_topology,
            now_provider=lambda: NOW)
    finally:
        Path.unlink = original_unlink
    exists_after_failure = retry_db.exists()
    retried = project_purge.execute_purge(
        {"project_id": "retry-cleanup", "intent_id": retry_intent,
         "actor": "operator/access24",
         "explicit_authorization": f"EXECUTE PURGE retry-cleanup {retry_intent}"},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        now_provider=lambda: NOW)
    ok(incomplete.get("purged") is False and exists_after_failure
       and retried.get("idempotent") is True and retried.get("database_removed") is True
       and not retry_db.exists(),
       "a failed database removal remains visible and an authorized retry completes cleanup")

    create_project("review-only")
    report = impact("review-only")
    review = project_purge.record_cleanup_review(
        {"project_id": "review-only", "decision": report["recommendation"]["action"],
         "impact_report_receipt": report["receipt"], "approved_by": "operator/access24",
         "approved_at": NOW, "rationale": "Current impact audit supports this decision."},
        access_repository=store.access_repository, project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        now_provider=lambda: NOW)
    ok(review.get("recorded") is True and review.get("mutated_project") is False
       and store.get_project_record("review-only")["lifecycle_status"] == "active",
       "operator-reviewed keep/consolidate/archive decisions persist without applying them")

    client = TestClient(app)
    create_project("rest-review")
    rest_report = impact("rest-review")
    rest = client.post("/api/projects/rest-review/cleanup-review", json={
        "decision": rest_report["recommendation"]["action"],
        "impact_report_receipt": rest_report["receipt"], "approved_at": NOW,
        "rationale": "REST contract fixture"})
    mcp = json.loads(mcp_server.record_project_cleanup_review(
        None, project="rest-review", decision=rest_report["recommendation"]["action"],
        impact_report_receipt_json=json.dumps(rest_report["receipt"]), approved_at=NOW,
        rationale="MCP idempotency fixture"))
    ok(rest.status_code == 200 and mcp.get("recorded") is True,
       "REST and MCP delegate to the same durable cleanup-review command")
    ok(_write_required_scopes("/api/projects/x/purge/intents") == ("write:system",)
       and _write_required_scopes("/api/projects/x/cleanup-review") == ("write:system",),
       "all destructive intent and review routes require system authority")

finally:
    db_connection._close_pooled_conns()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nACCESS-24 guarded purge: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
