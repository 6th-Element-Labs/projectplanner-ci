#!/usr/bin/env python3
"""ACCESS-21: REST/MCP parity and fail-closed Project Administration UI."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT
from scripts.frontend_test_source import read_frontend_source


TMP = tempfile.mkdtemp(prefix="access21-project-admin-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "access21-test-secret"

from fastapi.testclient import TestClient  # noqa: E402

import store  # noqa: E402
from app import app, _write_required_scopes  # noqa: E402
import mcp_server  # noqa: E402
from switchboard.application.commands.project_metadata import (  # noqa: E402
    PROJECT_METADATA_UPDATE_SCHEMA,
)
from switchboard.application.queries.project_admin import (  # noqa: E402
    PROJECT_ADMINISTRATION_SCHEMA,
)


ACTIVE = "admin-active"
ARCHIVE = "admin-archive"
HIDDEN = "admin-hidden"
EDITOR_TOKEN = "access21-editor-token-value"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


client = TestClient(app)

try:
    for project_id in (ACTIVE, ARCHIVE, HIDDEN):
        made = store.create_project(project_id, project_id=project_id, actor="access21-test")
        ok(made.get("created") is True, f"created {project_id} fixture")
        store.init_db(project_id)

    # Shared read contract: REST and MCP expose the same administration projection.
    rest_detail = client.get(f"/api/projects/{ACTIVE}")
    mcp_detail = json.loads(mcp_server.get_project(None, project=ACTIVE))
    ok(rest_detail.status_code == 200
       and rest_detail.json().get("schema") == PROJECT_ADMINISTRATION_SCHEMA,
       "REST get_project returns the shared administration contract")
    ok(mcp_detail == rest_detail.json(),
       "MCP get_project is byte-equivalent to REST in the same principal context")
    ok(set((rest_detail.json().get("access_summary") or {}).keys()) >= {
        "access", "grant_count", "role_counts", "principal_roles"}
       and "repo_topology" in rest_detail.json()
       and "lifecycle_events" in rest_detail.json(),
       "administration detail includes repo, access, and lifecycle receipt summaries")

    # Safe metadata uses write:projects and rejects lifecycle/trust-boundary fields.
    rest_update = client.patch(
        f"/api/projects/{ACTIVE}",
        json={"label": "Active Admin", "purpose": "Operator managed"},
    )
    ok(rest_update.status_code == 200
       and rest_update.json().get("schema") == PROJECT_METADATA_UPDATE_SCHEMA
       and rest_update.json().get("project", {}).get("label") == "Active Admin",
       "REST update_project persists safe ordinary metadata")
    mcp_update = json.loads(mcp_server.update_project(
        None, project=ACTIVE, metadata_json=json.dumps({"boundary": "Stay scoped"})))
    ok(mcp_update.get("project", {}).get("boundary") == "Stay scoped",
       "MCP update_project delegates to the same safe metadata command")
    unsafe_rest = client.patch(f"/api/projects/{ACTIVE}", json={"owner_user_id": "other"})
    unsafe_mcp = json.loads(mcp_server.update_project(
        None, project=ACTIVE, metadata_json=json.dumps({"lifecycle_status": "archived"})))
    ok(unsafe_rest.status_code == 400
       and unsafe_rest.json().get("detail", {}).get("error") == "unsafe_project_metadata_fields"
       and unsafe_mcp.get("error") == "unsafe_project_metadata_fields",
       "REST and MCP reject ownership/lifecycle fields instead of silently ignoring them")
    ok(_write_required_scopes(f"/api/projects/{ACTIVE}") == ("write:projects",)
       and _write_required_scopes(f"/api/projects/{ACTIVE}/archive") == ("write:system",)
       and _write_required_scopes(f"/api/projects/{ACTIVE}/restore") == ("write:system",),
       "HTTP middleware separates ordinary metadata from lifecycle authority")

    # Archive via exact receipt, prove active-only discovery and historical admin discovery.
    impact = client.get(f"/api/projects/{ARCHIVE}/impact").json()
    archived = client.post(
        f"/api/projects/{ARCHIVE}/archive",
        json={"reason": "ACCESS-21 admin fixture",
              "impact_report_receipt": impact.get("receipt")},
    )
    ok(archived.status_code == 200
       and archived.json().get("project", {}).get("lifecycle_status") == "archived",
       "archive action succeeds only against the displayed impact receipt")
    normal_ids = {p["id"] for p in client.get("/api/projects").json().get("projects", [])}
    admin_list = client.get("/api/projects", params={"include_archived": 1}).json()
    admin_records = {p["id"]: p for p in admin_list.get("projects", [])}
    ok(ARCHIVE not in normal_ids
       and admin_records.get(ARCHIVE, {}).get("lifecycle_status") == "archived",
       "normal picker remains active-only while explicit admin discovery includes archived")
    archived_update = client.patch(f"/api/projects/{ARCHIVE}", json={"purpose": "must deny"})
    ok(archived_update.status_code == 423
       and archived_update.json().get("detail", {}).get("error") == "project_archived",
       "archived metadata never optimistically succeeds")
    historical = client.get(f"/api/projects/{ARCHIVE}")
    ok(historical.status_code == 200
       and historical.json().get("project", {}).get("lifecycle_status") == "archived",
       "explicit authorized historical administration read remains available")
    restored = client.post(
        f"/api/projects/{ARCHIVE}/restore", json={"reason": "Restore after UI proof"})
    receipts = client.get(f"/api/projects/{ARCHIVE}").json().get("lifecycle_events") or []
    ok(restored.status_code == 200 and len(receipts) == 2
       and [event.get("to_status") for event in receipts] == ["archived", "active"],
       "restore validates and lifecycle receipts expose both audited transitions")

    # Required-mode principals cannot read another project or perform lifecycle work with
    # only write:projects. The denial occurs before any record or receipt is returned.
    store.create_principal(
        kind="agent", display_name="ACCESS-21 editor", token=EDITOR_TOKEN,
        scopes=["read", "write:projects"], project=ACTIVE,
    )
    os.environ["PM_AUTH_MODE"] = "required"
    own = client.get(f"/api/projects/{ACTIVE}", headers=bearer(EDITOR_TOKEN))
    hidden = client.get(f"/api/projects/{HIDDEN}", headers=bearer(EDITOR_TOKEN))
    editor_update = client.patch(
        f"/api/projects/{ACTIVE}", json={"purpose": "Scoped editor update"},
        headers=bearer(EDITOR_TOKEN),
    )
    editor_boundary = client.patch(
        f"/api/projects/{ACTIVE}", json={"boundary": "must deny"},
        headers=bearer(EDITOR_TOKEN),
    )
    editor_archive = client.post(
        f"/api/projects/{ACTIVE}/archive",
        json={"reason": "must deny", "impact_report_receipt": {}},
        headers=bearer(EDITOR_TOKEN),
    )
    ok(own.status_code == 200 and hidden.status_code in {401, 403},
       "inaccessible project detail never leaks across scoped principals")
    ok(editor_update.status_code == 200
       and editor_boundary.status_code == 403 and editor_archive.status_code == 403,
       "write:projects edits ordinary metadata but cannot cross access/lifecycle trust boundaries")
    denied_mcp = False
    try:
        mcp_server.get_project(None, project=HIDDEN)
    except (PermissionError, ValueError):
        denied_mcp = True
    ok(denied_mcp, "MCP get_project fails closed without an authorized context")
    os.environ["PM_AUTH_MODE"] = "dev-open"

    # Operator UI is a separate frontend boundary and renders exact blockers/receipts.
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "js" / "project-admin.js").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    # The settings surface that drives the project-admin card is its own module
    # (UI-18), so assert call-sites against the composed frontend rather than
    # app.js alone — otherwise the check pins a file, not the behaviour.
    composed = read_frontend_source(str(ROOT))
    ok('src="js/project-admin.js?v=' in index
       and "window.SwitchboardProjectAdmin.methods" in app_js,
       "operator screen composes the project administration frontend boundary")
    for needle in (
        "Active + archived", "Active only", "Archived only", "Save metadata",
        "Impact preview", "blocking_findings", "impact_report_receipt",
        "data-archive-allowed", "Protected project", "Lifecycle receipts",
        "Archiving against the displayed receipt", "Validating access and topology",
        "Archive is available", "Add a reason before archiving",
        "Confirm that you reviewed the current impact receipt",
        'role="status" aria-live="polite"', "Archived project (admin view)",
    ):
        ok(needle in ui, f"project administration UI exposes {needle}")
    ok("${canArchive ? '' : ' disabled'}" in ui
       and "${canRestore ? '' : ' disabled'}" in ui,
       "eligible lifecycle actions render enabled before confirmation")
    ok("element.checked && archive.dataset.archiveAllowed" not in ui
       and "element.checked && restore.dataset.restoreAllowed" not in ui,
       "confirmation no longer makes an available action look permanently disabled")
    ok("this._projectAdminSyncSwitcher();" in composed,
       "lifecycle refresh synchronizes the active-only project switcher")
    ok("await this._sSend" in ui and "catch (e)" in ui,
       "destructive-looking UI actions wait for server success and preserve visible failures")

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nACCESS-21 project administration: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
