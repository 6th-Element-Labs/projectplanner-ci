#!/usr/bin/env python3
"""ACCESS-23: receipt-gated, history-preserving project consolidation."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="access23-consolidation-")
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
from switchboard.application.commands import project_consolidation  # noqa: E402
from switchboard.contracts import (  # noqa: E402
    PROJECT_CONSOLIDATION_APPLY_COMMAND_SCHEMA,
    PROJECT_CONSOLIDATION_PLAN_COMMAND_SCHEMA,
    PROJECT_CONSOLIDATION_PLAN_SCHEMA,
    PROJECT_CONSOLIDATION_ROLLBACK_COMMAND_SCHEMA,
    get_schema,
)
from switchboard.domain.projects import ProjectLifecycleWriteBlocked  # noqa: E402


SOURCE = "legacy-initiative"
TARGET = "replacement-home"
STALE = "stale-initiative"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def create_project(project_id: str) -> None:
    made = store.create_project(
        project_id, project_id=project_id, actor="access23-test",
        purpose=f"{project_id} purpose", boundary=f"{project_id} boundary")
    assert made.get("created") is True, made
    store.init_db(project_id)


def add_done_task(project_id: str, task_id: str) -> None:
    with store._conn(project_id) as c:
        c.execute(
            "INSERT INTO tasks(task_id, workstream_id, workstream_name, title, status, "
            "depends_on, sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, "LEGACY", "Legacy", "Historical task", "Done", "[]", 0, 1.0, 1.0),
        )
        c.execute(
            "INSERT INTO task_git_state(task_id, merged_sha, merged_at, in_main_content, "
            "evidence_json, updated_at) VALUES (?,?,?,?,?,?)",
            (task_id, "merged-" + task_id.lower(), 1.0, 1,
             json.dumps({"proof": task_id}, sort_keys=True), 1.0),
        )


def approval():
    return {
        "decision": "consolidate",
        "approved_by": "operator/access23",
        "approved_at": 1_783_907_000.0,
        "rationale": "Legacy initiative belongs under the replacement deliverable.",
    }


def plan(project_id=SOURCE):
    return project_consolidation.plan_project_consolidation(
        {
            "source_project_id": project_id,
            "replacement_project_id": TARGET,
            "replacement_mission_id": "replacement-mission",
            "replacement_board_id": "replacement-mission",
            "replacement_deliverable_id": "replacement-deliverable",
            "safe_routing_keys": ["comms_notify_recipients"],
            "reason": "ACCESS-23 test consolidation",
            "actor": "operator/access23",
            "approval": approval(),
        },
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )


try:
    store.init_project_registry()
    for builtin in ("maxwell", "helm", "switchboard"):
        store.init_db(builtin)
    for project_id in (SOURCE, TARGET, STALE):
        create_project(project_id)

    add_done_task(SOURCE, "LEGACY-1")
    add_done_task(STALE, "STALE-1")
    store.create_project_board(
        {"id": "legacy-mission", "title": "Legacy mission", "kind": "mission",
         "status": "active"}, actor="fixture", project=SOURCE)
    store.create_project_board(
        {"id": "stale-mission", "title": "Stale mission", "kind": "mission",
         "status": "active"}, actor="fixture", project=STALE)
    store.create_project_board(
        {"id": "replacement-mission", "title": "Replacement mission", "kind": "mission",
         "status": "active"}, actor="fixture", project=TARGET)
    store.create_deliverable(
        {"id": "replacement-deliverable", "board_id": "replacement-mission",
         "title": "Replacement deliverable", "status": "in_progress"},
        actor="fixture", project=TARGET)
    linked = store.link_task_to_deliverable(
        "replacement-deliverable", SOURCE, "LEGACY-1",
        data={"role": "foundation"}, actor="fixture", project=TARGET)
    assert not linked.get("error"), linked
    store.set_meta("comms_notify_recipients", ["legacy@example.com"], project=SOURCE)
    store.set_meta("comms_notify_recipients", ["home@example.com"], project=TARGET)

    ok(all(get_schema(schema) is not None for schema in (
        PROJECT_CONSOLIDATION_PLAN_COMMAND_SCHEMA,
        PROJECT_CONSOLIDATION_APPLY_COMMAND_SCHEMA,
        PROJECT_CONSOLIDATION_ROLLBACK_COMMAND_SCHEMA,
    )), "plan, apply, and rollback command schemas are registered")

    first_plan = plan()
    second_plan = plan()
    ok(first_plan == second_plan
       and first_plan.get("schema") == PROJECT_CONSOLIDATION_PLAN_SCHEMA,
       "dry-run plan is deterministic and idempotent")
    ok(first_plan.get("approval", {}).get("approved_by") == "operator/access23"
       and first_plan.get("preservation_policy", {}).get("copy_tasks") is False
       and first_plan.get("preservation_policy", {}).get("fabricate_done_state") is False,
       "plan carries explicit operator approval and forbids task/status synthesis")
    ok(first_plan.get("inventory", {}).get("cross_project_links", {}).get("total") == 1
       and first_plan.get("history", {}).get("counts", {}).get("task_git_state") == 1,
       "plan inventories cross-project references and source provenance")

    unsafe = project_consolidation.plan_project_consolidation(
        {**{k: v for k, v in {
            "source_project_id": SOURCE, "replacement_project_id": TARGET,
            "reason": "unsafe", "actor": "operator/access23", "approval": approval(),
        }.items()}, "safe_routing_keys": ["repo_topology"]},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    ok(unsafe.get("error") == "unsafe_routing_keys",
       "routing rewrite allowlist rejects integration/topology mutation")
    unapproved = project_consolidation.plan_project_consolidation(
        {"source_project_id": SOURCE, "replacement_project_id": TARGET,
         "reason": "no approval", "actor": "agent", "approval": {}},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    ok(unapproved.get("error") == "invalid_project_consolidation_plan",
       "candidate classification cannot be agent-inferred or omitted")
    protected = project_consolidation.plan_project_consolidation(
        {"source_project_id": "switchboard", "replacement_project_id": TARGET,
         "reason": "must block", "actor": "operator", "approval": approval()},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    ok(protected.get("error") == "protected_project",
       "protected registry records cannot become consolidation sources")

    mismatch = project_consolidation.apply_project_consolidation(
        {"plan": first_plan, "confirmation": "wrong", "actor": "operator/access23"},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    ok(mismatch.get("error") == "confirmation_mismatch"
       and store.get_project_record(SOURCE)["lifecycle_status"] == "active",
       "typed confirmation fails closed before freezing writes")

    stale_plan = plan(STALE)
    add_done_task(STALE, "STALE-2")
    stale_apply = project_consolidation.apply_project_consolidation(
        {"plan": stale_plan, "confirmation": stale_plan.get("confirmation"),
         "actor": "operator/access23"},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    ok(stale_apply.get("error") == "stale_consolidation_plan"
       and store.get_project_record(STALE)["lifecycle_status"] == "active",
       "source changes invalidate a previously approved plan")

    client = TestClient(app)
    rest_apply = client.post(
        f"/api/projects/{SOURCE}/consolidation/apply",
        json={"plan": first_plan, "confirmation": first_plan["confirmation"]},
    )
    applied = rest_apply.json()
    record = applied.get("record") or {}
    consolidation_id = record.get("consolidation_id")
    ok(rest_apply.status_code == 200 and applied.get("applied") is True
       and applied.get("verification", {}).get("verified") is True,
       "REST apply freezes, records, and verifies the exact plan")
    source_record = store.get_project_record(SOURCE)
    ok(source_record.get("lifecycle_status") == "archived"
       and source_record.get("replacement_project_id") == TARGET
       and source_record.get("replacement_mission_id") == "replacement-mission"
       and source_record.get("replacement_deliverable_id") == "replacement-deliverable"
       and source_record.get("replacement_consolidation_id") == consolidation_id,
       "apply archives source and records every replacement pointer")
    ok(store.get_meta("comms_notify_recipients", [], project=TARGET)
       == ["home@example.com", "legacy@example.com"],
       "apply unions only the approved safe routing value")
    historical = store.get_task("LEGACY-1", project=SOURCE)
    target_deliverable = store.get_deliverable("replacement-deliverable", project=TARGET)
    ok(historical.get("status") == "Done"
       and historical.get("git_state", {}).get("merged_sha") == "merged-legacy-1"
       and f'"project_id": "{SOURCE}"' in json.dumps(target_deliverable, sort_keys=True),
       "archived source history and cross-project graph routing remain authoritative")
    denied = False
    try:
        store.create_task({"workstream_id": "DENIED", "title": "no write"}, project=SOURCE)
    except ProjectLifecycleWriteBlocked:
        denied = True
    ok(denied, "archive-as-freeze blocks every normal source write surface")

    repeated = project_consolidation.apply_project_consolidation(
        {"plan": first_plan, "confirmation": first_plan["confirmation"],
         "actor": "operator/access23"},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    ok(repeated.get("applied") is True and repeated.get("idempotent") is True,
       "repeated apply returns the durable receipt without duplicating work")
    rest_verify = client.get(
        f"/api/projects/{SOURCE}/consolidation/{consolidation_id}/verify")
    ok(rest_verify.status_code == 200 and rest_verify.json().get("verified") is True
       and all(rest_verify.json().get("checks", {}).values()),
       "verify proves history, pointers, routing, and archived graph resolution")

    rest_rollback = client.post(
        f"/api/projects/{SOURCE}/consolidation/{consolidation_id}/rollback",
        json={"reason": "pre-purge rollback drill"},
    )
    rolled = rest_rollback.json()
    restored_record = store.get_project_record(SOURCE)
    ok(rest_rollback.status_code == 200 and rolled.get("rolled_back") is True
       and str(rolled.get("receipt_hash") or "").startswith("sha256:"),
       "rollback emits a content-addressed pre-purge receipt")
    ok(restored_record.get("lifecycle_status") == "active"
       and restored_record.get("replacement_project_id") is None
       and restored_record.get("replacement_consolidation_id") is None
       and store.get_meta("comms_notify_recipients", [], project=TARGET)
       == ["home@example.com"],
       "rollback restores source writes, clears pointers, and restores target routing exactly")
    repeated_rollback = client.post(
        f"/api/projects/{SOURCE}/consolidation/{consolidation_id}/rollback",
        json={"reason": "repeat rollback"},
    ).json()
    ok(repeated_rollback.get("rolled_back") is True
       and repeated_rollback.get("idempotent") is True,
       "repeated rollback is idempotent")

    mcp_plan = json.loads(mcp_server.plan_project_consolidation(
        None, project=SOURCE, replacement_project=TARGET,
        replacement_mission="replacement-mission",
        replacement_board="replacement-mission",
        replacement_deliverable="replacement-deliverable",
        reason="MCP dry run", approval_json=json.dumps(approval()),
        safe_routing_keys_json="[]"))
    ok(mcp_plan.get("schema") == PROJECT_CONSOLIDATION_PLAN_SCHEMA
       and mcp_plan.get("source_project_id") == SOURCE,
       "MCP plan delegates to the shared consolidation application command")
    ok(_write_required_scopes(f"/api/projects/{SOURCE}/consolidation/plan")
       == ("write:system",)
       and _write_required_scopes(
           f"/api/projects/{SOURCE}/consolidation/{consolidation_id}/rollback")
       == ("write:system",),
       "consolidation plan/apply/rollback stay behind system authority")

finally:
    db_connection._close_pooled_conns()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nACCESS-23 project consolidation: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
