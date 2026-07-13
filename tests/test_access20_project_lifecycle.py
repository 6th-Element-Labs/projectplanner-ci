#!/usr/bin/env python3
"""ACCESS-20: receipt-gated archive, validated restore, and central write denial."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="access20-lifecycle-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_TOP_LEVEL_PROJECTS"] = "maxwell,helm,switchboard"

from fastapi.testclient import TestClient  # noqa: E402

import background_jobs  # noqa: E402
import db.connection as db_connection  # noqa: E402
import store  # noqa: E402
import webhook_inbox  # noqa: E402
from switchboard.application.commands import project_lifecycle  # noqa: E402
from switchboard.application.queries import project_impact  # noqa: E402
from switchboard.contracts import (  # noqa: E402
    ARCHIVE_PROJECT_COMMAND_SCHEMA,
    PROJECT_IMPACT_RECEIPT_SCHEMA,
    RESTORE_PROJECT_COMMAND_SCHEMA,
    get_schema,
)
from switchboard.domain.projects import (  # noqa: E402
    PROJECT_LIFECYCLE_WRITE_BLOCK_SCHEMA,
    ProjectLifecycleWriteBlocked,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def create_project(project_id: str) -> None:
    created = store.create_project(
        project_id, project_id=project_id, actor="fixture",
        purpose=f"{project_id} purpose", boundary=f"{project_id} boundary")
    assert created.get("created") is True, created
    store.init_db(project_id)


def impact(project_id: str) -> dict:
    return project_impact.execute_for(
        project_id,
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )


def archive(project_id: str, receipt: dict, reason: str = "fixture archive") -> dict:
    return project_lifecycle.archive_project(
        {"project_id": project_id, "reason": reason,
         "impact_report_receipt": receipt, "actor": "fixture"},
        access_repository=store.access_repository,
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )


def restore(project_id: str, reason: str = "fixture restore") -> dict:
    return project_lifecycle.restore_project(
        {"project_id": project_id, "reason": reason, "actor": "fixture"},
        access_repository=store.access_repository,
        repo_topology_provider=store.get_project_repo_topology,
    )


def blocked_call(fn) -> dict:
    try:
        fn()
    except ProjectLifecycleWriteBlocked as exc:
        return exc.detail
    return {}


try:
    store.init_project_registry()
    for builtin in ("maxwell", "helm", "switchboard"):
        store.init_db(builtin)

    ok(get_schema(ARCHIVE_PROJECT_COMMAND_SCHEMA) is not None
       and get_schema(RESTORE_PROJECT_COMMAND_SCHEMA) is not None
       and get_schema(PROJECT_IMPACT_RECEIPT_SCHEMA) is not None,
       "archive, restore, and impact receipt schemas are registered")

    # A terminal historical graph is archive-eligible and remains readable afterward.
    create_project("archive-ok")
    with store._conn("archive-ok") as c:
        c.execute(
            "INSERT INTO tasks(task_id, workstream_id, workstream_name, title, status, depends_on, "
            "sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("HIST-1", "HIST", "History", "Merged history", "Done", "[]", 0, 1.0, 1.0),
        )
        c.execute(
            "INSERT INTO task_git_state(task_id, merged_sha, merged_at, in_main_content, "
            "evidence_json, updated_at) VALUES (?,?,?,?,?,?)",
            ("HIST-1", "abc123", 1.0, 1, "{}", 1.0),
        )
        c.execute(
            "INSERT INTO project_boards(id, title, kind, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("history-board", "History", "mission", "archived", 1.0, 1.0),
        )
        c.execute(
            "INSERT INTO deliverables(id, board_id, title, status, acceptance_criteria_json, "
            "policy_constraints_json, proof_requirements_json, kpi_links_json, metadata_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("history-deliverable", "history-board", "History", "archived", "[]", "{}",
             "{}", "[]", "{}", 1.0, 1.0),
        )

    report = impact("archive-ok")
    ok(report["receipt"]["schema"] == PROJECT_IMPACT_RECEIPT_SCHEMA
       and report["recommendation"]["actions"]["archive"]["eligible"] is True,
       "terminal historical state produces an exact archive-eligible receipt")
    first = archive("archive-ok", report["receipt"])
    ok(first.get("transitioned") is True
       and first.get("project", {}).get("lifecycle_status") == "archived",
       "archive command commits the active-to-archived transition")
    ok("archive-ok" not in {p["id"] for p in store.projects()}
       and store.has_project("archive-ok"),
       "archive hides default discovery while preserving explicit routing")
    ok(store.get_task("HIST-1", project="archive-ok").get("git_state", {}).get("merged_sha") == "abc123"
       and store.get_deliverable("history-deliverable", project="archive-ok")["status"] == "archived"
       and store.get_project_board("history-board", project="archive-ok")["status"] == "archived",
       "historical tasks, merge provenance, deliverables, and boards remain readable")
    ok(impact("archive-ok").get("project", {}).get("lifecycle_status") == "archived",
       "impact and explicit historical reads remain available after archive")

    second = archive("archive-ok", report["receipt"])
    ok(second.get("idempotent") is True and second.get("transitioned") is False,
       "repeated archive is idempotent")
    wrong_receipt = {**report["receipt"], "project_id": "somewhere-else"}
    ok(archive("archive-ok", wrong_receipt).get("error") == "invalid_impact_report_receipt",
       "idempotent archive still rejects a receipt issued for another project")

    # One connection/write-through guard covers representative store and worker surfaces.
    denials = [
        blocked_call(lambda: store.create_task(
            {"workstream_id": "NEW", "title": "denied"}, project="archive-ok")),
        blocked_call(lambda: store.create_deliverable(
            {"title": "denied"}, project="archive-ok")),
        blocked_call(lambda: store.create_project_board(
            {"title": "denied"}, project="archive-ok")),
        blocked_call(lambda: store.claim_task(
            "HIST-1", "agent/denied", project="archive-ok")),
        blocked_call(lambda: store.create_work_session({
            "agent_id": "agent/denied", "runtime": "test", "repo_role": "canonical",
            "storage_mode": "external", "status": "active", "dirty_status": "clean",
        }, project="archive-ok")),
        blocked_call(lambda: store.create_principal(
            "agent", "Denied", "secret", ["read"], project="archive-ok")),
        blocked_call(lambda: store.add_inbox_item(
            "test", "denied-1", "sender", "subject", "summary", {},
            project="archive-ok")),
        blocked_call(lambda: webhook_inbox.enqueue_event(
            "archive-ok", delivery_guid="denied-webhook", event="push",
            payload_bytes=b"{}")),
        blocked_call(lambda: background_jobs.run_background_job(
            "archive-ok", "receipt_projection_batch", resume=False)),
        blocked_call(lambda: store.set_project_repo_topology(
            project="archive-ok", canonical_repo="owner/repo")),
        blocked_call(lambda: store.append_activity(
            "denied", "fixture", project="archive-ok")),
    ]
    ok(all(item.get("schema") == PROJECT_LIFECYCLE_WRITE_BLOCK_SCHEMA
           and item.get("error") == "project_archived" for item in denials),
       "central guard rejects task, deliverable, board, claim, session, token, webhook, "
       "inbox, background-job, integration, and activity writes")
    with store._conn("archive-ok") as archived_conn:
        pragma_rows = archived_conn.execute("PRAGMA table_info(tasks)").fetchall()
        cte_denied = blocked_call(lambda: archived_conn.execute(
            "WITH candidate AS (SELECT 'X') INSERT INTO meta(key,value) "
            "SELECT 'cte-denied','1' FROM candidate"))
        comment_denied = blocked_call(lambda: archived_conn.execute(
            "-- leading comment\nINSERT INTO meta(key,value) VALUES ('comment-denied','1')"))
        pragma_denied = blocked_call(lambda: archived_conn.execute("PRAGMA user_version=9"))
        bare_pragma_denied = blocked_call(lambda: archived_conn.execute("PRAGMA optimize"))
        analyze_denied = blocked_call(lambda: archived_conn.execute("ANALYZE"))
        cursor_denied = blocked_call(lambda: archived_conn.cursor().execute(
            "INSERT INTO meta(key,value) VALUES ('cursor-denied','1')"))
        chained_denied = blocked_call(lambda: archived_conn.execute(
            "SELECT 1").execute(
                "INSERT INTO meta(key,value) VALUES ('chained-denied','1')"))
    ok(bool(pragma_rows)
       and all(item.get("error") == "project_archived"
               for item in (cte_denied, comment_denied, pragma_denied,
                            bare_pragma_denied, analyze_denied, cursor_denied,
                            chained_denied)),
       "historical query pragmas remain readable while SQL, cursor, and PRAGMA writes deny")
    role_denied = store.grant_project_role(
        "archive-ok", "principal", "principal-x", "viewer", created_by="fixture")
    ok(role_denied.get("error") == "project_archived",
       "registry-only project role writes use the same lifecycle guard")

    # Restore validates access/topology, reopens writes, and audits both transitions.
    restored = restore("archive-ok")
    ok(restored.get("transitioned") is True
       and restored.get("project", {}).get("lifecycle_status") == "active",
       "restore reopens the project after access and topology validation")
    new_task = store.create_task(
        {"workstream_id": "NEW", "title": "writes reopened"}, project="archive-ok")
    ok(new_task.get("task_id", "").startswith("NEW-"),
       "successful restore reopens normal writes")
    repeated_restore = restore("archive-ok")
    ok(repeated_restore.get("idempotent") is True,
       "repeated restore is idempotent")
    events = store.access_repository.list_project_lifecycle_events("archive-ok")
    ok([event["to_status"] for event in events] == ["archived", "active"]
       and events[0]["impact_report_hash"] == report["receipt"]["report_hash"]
       and events[1]["validation"]["access_valid"] is True,
       "archive and restore transitions have durable registry audit evidence")
    audit_registry = store.audit_export(project="archive-ok")["access"]
    ok([event["to_status"] for event in audit_registry["project_lifecycle_events"]]
       == ["archived", "active"],
       "enterprise audit export includes lifecycle transition evidence")

    # Stale receipts and current blockers fail closed without changing lifecycle state.
    create_project("stale-audit")
    stale_receipt = impact("stale-audit")["receipt"]
    store.create_task({"workstream_id": "LIVE", "title": "changed"}, project="stale-audit")
    stale = archive("stale-audit", stale_receipt)
    ok(stale.get("error") == "stale_impact_report_receipt"
       and store.get_project_record("stale-audit")["lifecycle_status"] == "active",
       "state changes invalidate an old impact receipt")
    current_blocked_report = impact("stale-audit")
    blocked = archive("stale-audit", current_blocked_report["receipt"])
    ok(blocked.get("error") == "project_archive_blocked"
       and blocked.get("blocking_findings"),
       "a current receipt cannot bypass impact blockers")
    missing_reason = archive("stale-audit", current_blocked_report["receipt"], reason="")
    ok(missing_reason.get("error") == "invalid_archive_project_command",
       "archive requires an explicit non-empty reason")
    direct = store.update_project_metadata(
        {"project_id": "stale-audit", "lifecycle_status": "archived"}, actor="fixture")
    ok(direct.get("error") == "lifecycle_command_required",
       "generic metadata updates cannot bypass the receipt gate")

    create_project("race-audit")
    race_report = impact("race-audit")

    class RacingAccessRepository:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def transition_project_lifecycle(self, project_id, requested, **kwargs):
            if requested == "archived":
                db_path = store._project_map()[project_id]["db"]
                with sqlite3.connect(db_path) as raw:
                    raw.execute(
                        "INSERT INTO tasks(task_id, workstream_id, title, status, depends_on, "
                        "sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        ("RACE-1", "RACE", "late writer", "Not Started", "[]", 0, 2.0, 2.0),
                    )
            return self.delegate.transition_project_lifecycle(
                project_id, requested, **kwargs)

    race_result = project_lifecycle.archive_project(
        {"project_id": "race-audit", "reason": "race fixture",
         "impact_report_receipt": race_report["receipt"], "actor": "fixture"},
        access_repository=RacingAccessRepository(store.access_repository),
        project_configs=store._project_map(),
        registry_db_path=store.PROJECT_REGISTRY_DB_PATH,
        repo_topology_provider=store.get_project_repo_topology,
    )
    ok(race_result.get("error") == "project_changed_during_archive"
       and race_result.get("rollback", {}).get("to_status") == "active"
       and store.get_project_record("race-audit")["lifecycle_status"] == "active",
       "a writer racing the receipt transition is detected and the archive rolls back")

    # Cross-process cache coherence: a registry transition is visible without waiting for TTL.
    create_project("cache-audit")
    store._project_map()
    child = (
        "import store; "
        "r=store.access_repository.transition_project_lifecycle("
        "'cache-audit','archived',actor='child',reason='cross-process',"
        "impact_report_hash='sha256:child'); "
        "raise SystemExit(0 if r.get('transitioned') else 1)"
    )
    child_run = subprocess.run(
        [os.environ.get("PYTHON", "python3"), "-c", child], cwd=str(ROOT),
        env=dict(os.environ), capture_output=True, text=True)
    if child_run.returncode != 0:
        print("  DETAIL child stdout=" + child_run.stdout.strip())
        print("  DETAIL child stderr=" + child_run.stderr.strip())
    ok(child_run.returncode == 0
       and store._project_map()["cache-audit"]["lifecycle_status"] == "archived",
       "registry WAL signature invalidates lifecycle cache across processes")

    # REST and MCP use the same commands; generic REST writes surface HTTP 423.
    from app import app  # noqa: E402
    client = TestClient(app)
    rest_denied = client.post(
        "/api/tasks", params={"project": "cache-audit"},
        json={"workstream_id": "REST", "title": "denied"})
    ok(rest_denied.status_code == 423
       and rest_denied.json()["detail"]["error"] == "project_archived",
       "REST maps the central lifecycle denial to an explicit locked response")
    webhook_denied = client.post(
        "/api/github/webhook", params={"project": "cache-audit"},
        headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "rest-denied"},
        json={"repository": {"full_name": "owner/repo"}, "ref": "refs/heads/main"})
    ok(webhook_denied.status_code == 423
       and webhook_denied.json()["detail"]["operation"] == "insert",
       "archived webhook ingestion is rejected before a durable inbox write")

    create_project("api-audit")
    api_receipt = impact("api-audit")["receipt"]
    api_archive = client.post(
        "/api/projects/api-audit/archive",
        json={"reason": "REST archive", "impact_report_receipt": api_receipt})
    api_restore = client.post(
        "/api/projects/api-audit/restore", json={"reason": "REST restore"})
    ok(api_archive.status_code == 200 and api_archive.json()["action"] == "archive"
       and api_restore.status_code == 200 and api_restore.json()["action"] == "restore",
       "REST archive and restore delegate to the shared application commands")

    import mcp_server  # noqa: E402
    mcp_write_denied = blocked_call(lambda: mcp_server.create_task(
        "MCP", "denied", None, project="cache-audit"))
    ok(mcp_write_denied.get("error") == "project_archived",
       "ordinary MCP writes are rejected by the same central lifecycle guard")
    create_project("mcp-audit")
    mcp_receipt = impact("mcp-audit")["receipt"]
    mcp_archive = json.loads(mcp_server.archive_project(
        None, project="mcp-audit", reason="MCP archive",
        impact_report_receipt_json=json.dumps(mcp_receipt)))
    mcp_restore = json.loads(mcp_server.restore_project(
        None, project="mcp-audit", reason="MCP restore"))
    ok(mcp_archive.get("action") == "archive" and mcp_archive.get("transitioned") is True
       and mcp_restore.get("action") == "restore" and mcp_restore.get("transitioned") is True,
       "MCP archive and restore delegate to the shared application commands")
    no_project = json.loads(mcp_server.archive_project(
        None, project="", reason="bad", impact_report_receipt_json="{}"))
    ok(no_project.get("error") == "project is required"
       and store.get_project_record("maxwell")["lifecycle_status"] == "active",
       "missing lifecycle routing never falls back silently to the default project")

finally:
    db_connection._close_pooled_conns()
    shutil.rmtree(TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
