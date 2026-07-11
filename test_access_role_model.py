#!/usr/bin/env python3
"""ACCESS-2 org/user/project role model regression."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="access-role-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
os.environ["PM_JWT_SECRET"] = "test-secret-do-not-use-in-prod"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import auth  # noqa: E402
    import store  # noqa: E402
    from app import app  # noqa: E402
    from services.auth import store as auth_store  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  ACCESS role-model smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "switchboard"
ADMIN_EMAIL = "admin@test.com"
ADMIN_PASSWORD = "role-model-admin-2026"
VIEWER_TOKEN = "viewer-token"
NOROLE_TOKEN = "norole-token"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    client = TestClient(app)
    auth_store.init()
    admin = auth_store.create_user(
        ADMIN_EMAIL, "Admin", auth.password_hash(ADMIN_PASSWORD), is_superadmin=True)
    store.ensure_bootstrap_project_owner(P, admin["id"], "admin", "Admin", actor="test")
    ok(client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 200,
       "global admin login succeeds")
    ok(admin.get("is_superadmin"), "bootstrap admin is superadmin")
    ok(any(r["role"] == "admin" for r in store.principal_project_roles(P, admin["id"])),
       "bootstrap admin receives project admin role")

    model = client.get("/api/access/model", params={"project": P})
    body = model.json()
    ok(model.status_code == 200 and body["access"]["org_id"] == store.DEFAULT_ORG_ID,
       "access model records default owning org")
    ok(body["access"]["owner_user_id"] == admin["id"],
       "access model records project owner user")
    ok(any(g["role"] == "admin" and g["subject_id"] == admin["id"] for g in body["grants"]),
       "access model exposes the admin grant")

    viewer = store.create_principal(
        kind="user",
        display_name="viewer",
        token=VIEWER_TOKEN,
        scopes=[],
        principal_id="user-viewer",
        project=P,
    )
    no_role = store.create_principal(
        kind="user",
        display_name="no-role",
        token=NOROLE_TOKEN,
        scopes=[],
        principal_id="user-norole",
        project=P,
    )
    grant = client.post(
        "/api/access/project_role",
        params={"project": P},
        json={"subject_kind": "principal", "subject_id": viewer["id"], "role": "viewer"},
    )
    ok(grant.status_code == 200 and grant.json()["role"] == "viewer",
       "admin can grant a viewer project role")

    viewer_headers = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
    viewer_read = client.get("/api/board", params={"project": P}, headers=viewer_headers)
    ok(viewer_read.status_code == 200, "viewer role grants project read access")
    viewer_write = client.post(
        f"/api/tasks?project={P}",
        json={"workstream_id": "ACCESS", "title": "viewer should not write"},
        headers=viewer_headers,
    )
    ok(viewer_write.status_code == 403, "viewer role does not grant task write access")

    no_role_read = client.get(
        "/api/board",
        params={"project": P},
        headers={"Authorization": f"Bearer {NOROLE_TOKEN}"},
    )
    ok(no_role_read.status_code == 403, "principal without scopes or role cannot read project")

    contributor_grant = client.post(
        "/api/access/project_role",
        params={"project": P},
        json={"subject_kind": "principal", "subject_id": viewer["id"], "role": "contributor"},
    )
    ok(contributor_grant.status_code == 200, "admin can upgrade viewer to contributor")
    contributor_write = client.post(
        f"/api/tasks?project={P}",
        json={"workstream_id": "ACCESS", "title": "contributor can write"},
        headers=viewer_headers,
    )
    ok(contributor_write.status_code == 200 and contributor_write.json()["title"] == "contributor can write",
       "contributor role grants task write access")

    bad_role = client.post(
        "/api/access/project_role",
        params={"project": P},
        json={"subject_kind": "principal", "subject_id": no_role["id"], "role": "made-up"},
    )
    ok(bad_role.status_code == 400, "unknown roles fail closed")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
