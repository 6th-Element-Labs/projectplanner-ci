#!/usr/bin/env python3
"""ACCESS-19: bounded, access-controlled project impact audit fixtures."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="access19-impact-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_TOP_LEVEL_PROJECTS"] = "maxwell,helm,switchboard"

from fastapi.testclient import TestClient  # noqa: E402

import store  # noqa: E402
import db.connection as db_connection  # noqa: E402
from switchboard.application.queries import project_impact  # noqa: E402
from switchboard.contracts import (  # noqa: E402
    PROJECT_IMPACT_REPORT_SCHEMA,
    PROJECT_IMPACT_RECEIPT_SCHEMA,
    get_schema,
)
from switchboard.storage.repositories.project_impact import ReadOnlyDatabase  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def insert_task(project: str, task_id: str, status: str = "Not Started",
                depends_on: list[str] | None = None) -> None:
    now = 1000.0 + len(task_id)
    with store._conn(project) as c:
        c.execute(
            "INSERT INTO tasks(task_id, workstream_id, workstream_name, title, status, depends_on, "
            "sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, task_id.split("-", 1)[0], "Fixture", task_id, status,
             json.dumps(depends_on or []), 0, now, now),
        )


def logical_dump(path: str) -> str:
    with sqlite3.connect(path) as c:
        return "\n".join(c.iterdump())


def report(project: str, limit: int = 50):
    return project_impact.execute_for(
        project,
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
        limit=limit,
    )


try:
    store.init_project_registry()
    for project_id in ("maxwell", "helm", "switchboard"):
        store.init_db(project_id)
    for project_id in ("empty-audit", "active-audit", "linked-audit",
                       "missing-audit", "corrupt-audit"):
        created = store.create_project(project_id, project_id=project_id, actor="fixture")
        ok(created.get("created") is True, f"created {project_id} fixture")
        store.init_db(project_id)

    # Empty fixture: initialized schema/access metadata, no operational contents.
    before_empty_db = logical_dump(store._project_map()["empty-audit"]["db"])
    before_registry = logical_dump(store.PROJECT_REGISTRY_DB_PATH)
    empty = report("empty-audit", limit=2)
    after_empty_db = logical_dump(store._project_map()["empty-audit"]["db"])
    after_registry = logical_dump(store.PROJECT_REGISTRY_DB_PATH)
    ok(empty.get("schema") == PROJECT_IMPACT_REPORT_SCHEMA,
       "empty fixture returns versioned impact contract")
    ok(empty.get("receipt", {}).get("schema") == PROJECT_IMPACT_RECEIPT_SCHEMA
       and empty["receipt"].get("project_id") == "empty-audit"
       and empty["receipt"].get("report_hash", "").startswith("sha256:"),
       "impact report carries a content-addressed archive receipt")
    ok(empty["tasks"]["total"] == 0 and empty["recommendation"]["action"] == "archive",
       "empty fixture is an archive candidate")
    ok(before_empty_db == after_empty_db and before_registry == after_registry,
       "impact report performs no logical database mutation")

    # Active fixture: bounded samples and every operational category represented.
    for task_id in ("ACTIVE-1", "ACTIVE-2", "ACTIVE-3"):
        insert_task("active-audit", task_id)
    insert_task("active-audit", "ACTIVE-9", status="Done", depends_on=["LINKED-1"])
    insert_task("linked-audit", "LINKED-1", depends_on=["ACTIVE-1"])
    with store._conn("active-audit") as c:
        c.execute(
            "INSERT INTO task_git_state(task_id, branch, head_sha, pr_number, pr_url, "
            "in_main_content, evidence_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("ACTIVE-1", "codex/active", "abc", 7, "https://example.test/pr/7", 0, "{}", 1100.0),
        )
        c.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, status, claimed_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            ("claim-active", "ACTIVE-1", "codex/active", "active", 1101.0, 9999999999.0),
        )
        c.execute(
            "INSERT INTO work_sessions(work_session_id, project_id, task_id, agent_id, repo_role, "
            "branch, upstream, base_sha, head_sha, worktree_path, storage_mode, status, dirty_status, "
            "conflict_marker_count, hygiene_json, file_leases_json, resource_leases_json, env_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ws-active", "active-audit", "ACTIVE-1", "codex/active", "canonical",
             "codex/ACTIVE-1-work", "origin/master", "base", "head", "/tmp/active", "worktree",
             "active", "clean", 0, "{}", "[]", "[]", "{}", 1102.0, 1102.0),
        )
        c.execute(
            "INSERT INTO project_boards(id, title, kind, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)", ("mission-active", "Mission", "mission", "active", 1103.0, 1103.0))
        c.execute(
            "INSERT INTO deliverables(id, title, status, acceptance_criteria_json, "
            "policy_constraints_json, proof_requirements_json, kpi_links_json, metadata_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("deliverable-active", "Deliverable", "active", "[]", "{}", "{}", "[]", "{}",
             1104.0, 1104.0),
        )
        c.execute(
            "INSERT INTO principals(id, kind, display_name, project, scopes, token_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("principal-active", "agent", "Active", "active-audit", '["read"]', "not-a-token", 1105.0),
        )
        c.execute(
            "INSERT INTO agent_messages(from_agent, to_agent, task_id, message, requires_ack, sent_at) "
            "VALUES (?,?,?,?,?,?)",
            ("one", "two", "ACTIVE-1", "fixture", 1, 1106.0),
        )
        c.execute(
            "INSERT INTO background_job_runs(run_id, job_name, project, status, runtime, manifest_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("job-active", "fixture", "active-audit", "running", "test", "{}", 1107.0, 1107.0),
        )
    store.set_meta("comms_inbound_domains", ["example.test"], project="active-audit")
    store.set_meta("comms_notify_recipients", ["ops@example.test"], project="active-audit")

    active = report("active-audit", limit=2)
    active_again = report("active-audit", limit=2)
    finding_codes = {item["code"] for item in active["blocking_findings"]}
    ok(active == active_again, "identical persisted state returns byte-equivalent report data")
    ok(active["tasks"]["nonterminal"]["total"] == 3
       and active["tasks"]["nonterminal"]["returned"] == 2
       and active["tasks"]["nonterminal"]["truncated"] is True,
       "active fixture samples are deterministically bounded")
    ok(active["cross_project_links"]["inbound"]["total"] == 1
       and active["cross_project_links"]["outbound"]["total"] == 1,
       "cross-linked fixture reports inbound and outbound task dependencies")
    ok({"nonterminal_work", "open_pr_provenance", "active_claims",
        "active_work_sessions", "hosted_outcomes", "cross_project_links",
        "active_credentials", "pending_communications", "active_automation"}.issubset(finding_codes),
       "active fixture produces archive-blocking findings across required surfaces")
    ok(active["recommendation"]["action"] == "keep"
       and active["recommendation"]["actions"]["archive"]["eligible"] is False,
       "active fixture recommends keep and blocks archive")

    protected = report("switchboard")
    ok(protected["project"]["is_protected"] is True
       and "protected_project" in {item["code"] for item in protected["blocking_findings"]}
       and protected["recommendation"]["action"] == "keep",
       "protected fixture fails closed with a keep recommendation")

    unknown = report("does-not-exist")
    ok(unknown.get("error") == "unknown project: does-not-exist",
       "unknown project fails closed")
    ok(get_schema(PROJECT_IMPACT_REPORT_SCHEMA) is not None,
       "impact report schema is registered")
    ok(get_schema(PROJECT_IMPACT_RECEIPT_SCHEMA) is not None,
       "impact receipt schema is registered")

    # REST and MCP share the same application query and enforce read access.
    from app import app  # noqa: E402
    client = TestClient(app)
    rest = client.get("/api/projects/active-audit/impact", params={"limit": 2})
    ok(rest.status_code == 200 and rest.json() == active,
       "REST surface returns the shared deterministic report")
    os.environ["PM_AUTH_MODE"] = "required"
    denied_rest = client.get("/api/projects/active-audit/impact")
    ok(denied_rest.status_code == 401, "REST report denies unauthenticated required-mode reads")

    import mcp_server  # noqa: E402
    denied_mcp = False
    try:
        mcp_server.get_project_impact_report(None, project="active-audit", limit=2)
    except ValueError:
        denied_mcp = True
    ok(denied_mcp, "MCP report denies unauthenticated required-mode reads")
    os.environ["PM_AUTH_MODE"] = "dev-open"
    mcp_payload = json.loads(mcp_server.get_project_impact_report(
        None, project="active-audit", limit=2))
    ok(mcp_payload == active, "MCP surface returns the shared deterministic report")

    # Missing, unreadable, and corrupt sources fail closed instead of looking empty.
    db_connection._close_pooled_conns()
    missing_path = store._project_map()["missing-audit"]["db"]
    for suffix in ("", "-wal", "-shm"):
        Path(missing_path + suffix).unlink(missing_ok=True)
    missing = report("missing-audit")
    missing_codes = {item["code"] for item in missing["blocking_findings"]}
    ok(missing["storage"]["database_read"]["error_code"] == "database_missing"
       and "snapshot_incomplete" in missing_codes,
       "missing project database produces an explicit incomplete-snapshot blocker")
    ok(missing["recommendation"]["action"] == "keep"
       and missing["recommendation"]["actions"]["archive"]["eligible"] is False,
       "missing project database can never produce an archive recommendation")

    corrupt_path = store._project_map()["corrupt-audit"]["db"]
    for suffix in ("-wal", "-shm"):
        Path(corrupt_path + suffix).unlink(missing_ok=True)
    Path(corrupt_path).write_bytes(b"not a sqlite database")
    corrupt = report("corrupt-audit")
    ok(corrupt["storage"]["database_read"]["available"] is False
       and corrupt["storage"]["database_read"]["error_code"] in {
           "database_unreadable", "database_read_failed"}
       and "snapshot_incomplete" in {item["code"] for item in corrupt["blocking_findings"]},
       "corrupt project database fails closed")

    def deny_open(*_args, **_kwargs):
        raise PermissionError("fixture")

    with ReadOnlyDatabase(store._project_map()["empty-audit"]["db"],
                          connector=deny_open) as unreadable_probe:
        unreadable_status = unreadable_probe.read_status()
    ok(unreadable_status == {
        "available": False,
        "error_code": "database_unreadable",
        "error_type": "PermissionError",
    }, "unreadable database status is explicit and non-empty")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
