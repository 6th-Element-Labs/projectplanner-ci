#!/usr/bin/env python3
"""UI-9: prove the project & provenance admin REST contract + the operator settings UI.

Covers the endpoints the admin Settings tab drives — repo topology read/write,
move task, verify offline completion, reconcile + reconcile alerts, and the
read-only external-CI / publication-evidence views — plus the app.js/index.html
surface that consumes them. Auth gating (write:system) is proven separately in
test_web_write_auth.py; here we run dev-open and assert the contract + UI wiring.
"""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ui9-admin-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-do-not-use-in-prod"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
from services.auth import store as auth_store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
    import auth  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  UI-9 admin proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

HOME = "qa-admin-home"
DEST = "qa-admin-dest"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


client = TestClient(app)

try:
    store.init_project_registry()
    store.create_project("Admin Home", project_id=HOME, actor="test")
    store.create_project("Admin Dest", project_id=DEST, actor="test")
    store.init_db(HOME)
    store.init_db(DEST)

    # --- who am I: the frontend gates the Settings tab on this ---
    auth_store.init()
    auth_store.create_user("admin@test.com", "Admin", auth.password_hash("admin-pass-2026"),
                           is_superadmin=True)
    client.post("/api/auth/login", json={"email": "admin@test.com", "password": "admin-pass-2026"})
    me = client.get("/api/auth/session")
    ok(me.status_code == 200, "GET /api/auth/session returns 200 when logged in")
    ok((me.json() or {}).get("user", {}).get("is_superadmin"),
       "auth/session exposes superadmin for admin-tab gating")

    # --- repo topology: read, then write, then read-back ---
    topo = client.get(f"/api/projects/{HOME}/repo_topology")
    ok(topo.status_code == 200, "GET repo_topology returns 200")
    roles = (topo.json() or {}).get("roles") or {}
    ok(set(["canonical", "public_ci", "public", "release"]).issubset(roles.keys()),
       "repo_topology exposes canonical/public_ci/public/release roles")

    set_topo = client.post(
        f"/api/projects/{HOME}/repo_topology",
        json={"canonical_repo": "acme/widget", "canonical_default_branch": "main",
              "public_ci_repo": "acme/widget-ci",
              "topology_type": "private_canonical_public_ci"},
    )
    ok(set_topo.status_code == 200, "POST repo_topology returns 200")
    back = client.get(f"/api/projects/{HOME}/repo_topology").json()
    ok(((back.get("roles") or {}).get("canonical") or {}).get("repo") == "acme/widget",
       "repo topology write persisted the canonical repo")

    # --- verify offline completion: stamp a non-PR task Done with evidence ---
    off_task = store.create_task(
        {"workstream_id": "OPS", "title": "Offline deliverable"},
        actor="test", project=HOME)
    # Offline Done requires the task to be In Review first (the UI surfaces the 409 if not).
    store.update_task(off_task["task_id"], {"status": "In Review"},
                      actor="test", project=HOME)
    vo = client.post(
        f"/api/tasks/{off_task['task_id']}/verify_offline",
        params={"project": HOME},
        json={"artifact_url": "https://example.com/report.pdf",
              "evidence": {"note": "reviewed by ops"}, "verifier": "qa-verifier"},
    )
    ok(vo.status_code == 200, "POST verify_offline returns 200")
    ok((vo.json() or {}).get("status") == "Done",
       "verify_offline stamps the task Done with recorded evidence")

    # --- move task cross-project ---
    mv_task = store.create_task(
        {"workstream_id": "OPS", "title": "Relocatable task"},
        actor="test", project=HOME)
    mv = client.post(
        f"/api/tasks/{mv_task['task_id']}/move",
        params={"project": HOME},
        json={"project_to": DEST, "dependency_policy": "fail"},
    )
    ok(mv.status_code == 200, "POST move_task returns 200")
    ok((mv.json() or {}).get("moved") is True and (mv.json() or {}).get("project_to") == DEST,
       "move_task relocated the task to the destination project")
    ok(store.get_task(mv_task["task_id"], project=DEST) is not None,
       "moved task now lives on the destination project")

    # --- reconcile (trigger + findings) ---
    rec = client.get("/ixp/v1/reconcile", params={"project": HOME})
    ok(rec.status_code == 200, "GET reconcile returns 200")
    rbody = rec.json() or {}
    ok(isinstance(rbody.get("findings"), list) and "ok" in rbody,
       "reconcile returns an ok flag + findings list for the drift panel")

    alerts = client.post("/ixp/v1/reconcile_alerts",
                         json={"project": HOME, "min_severity": "medium"})
    ok(alerts.status_code == 200, "POST reconcile_alerts returns 200")
    ok("finding_count" in (alerts.json() or {}),
       "reconcile_alerts returns a finding_count for the send-alert control")

    # --- read-only provenance views ---
    ci = client.get("/ixp/v1/external_ci_runs", params={"project": HOME})
    ok(ci.status_code == 200 and isinstance((ci.json() or {}).get("runs"), list),
       "GET external_ci_runs returns a runs list")
    pubs = client.get("/ixp/v1/publication_evidence", params={"project": HOME})
    ok(pubs.status_code == 200 and isinstance((pubs.json() or {}).get("publication_evidence"), list),
       "GET publication_evidence returns an evidence list")

    # --- the operator UI surface ---
    index = client.get("/")
    ok(index.status_code == 200, "index.html serves")
    for needle in ("nav-settings", "toptab-settings", 'id="tab-settings"', 'id="settings-page"'):
        ok(needle in index.text, f"index.html exposes {needle}")

    app_js = open(os.path.join(os.path.dirname(__file__), "static", "app.js"),
                  encoding="utf-8").read()
    for needle in (
        "loadPrincipal",
        "renderSettings",
        "_settingsRepoCard",
        "saveRepoTopology",
        "reconcileNow",
        "sendReconcileAlerts",
        "verifyOffline",
        "moveTask",
        "_settingsCiRunsCard",
        "_settingsPublicationCard",
        "data-set-action",
    ):
        ok(needle in app_js, f"app.js defines {needle}")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nUI-9 admin proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
