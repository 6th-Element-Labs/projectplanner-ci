#!/usr/bin/env python3
"""ACCESS-3 scoped MCP/API token regression."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="access-token-")
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
    import scripts.switchboard_path  # noqa: E402,F401
    from switchboard.api.routers.auth import store as auth_store  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  ACCESS scoped-token smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "switchboard"
EMAIL = "admin@test.com"
PASSWORD = "scoped-token-admin-2026"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def authz(token):
    return {"Authorization": f"Bearer {token}"}


try:
    client = TestClient(app)
    auth_store.init()
    auth_store.create_user(EMAIL, "Admin", auth.password_hash(PASSWORD), is_superadmin=True)
    ok(client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 200,
       "global admin login succeeds")

    create_viewer = client.post(
        "/api/access/tokens",
        params={"project": P},
        json={"kind": "agent", "display_name": "viewer adapter", "role": "viewer"},
    )
    body = create_viewer.json()
    viewer_token = body.get("token")
    viewer_principal = body.get("principal") or {}
    ok(create_viewer.status_code == 200 and viewer_token,
       "admin can create a viewer-scoped bearer token")
    ok(body.get("token_returned_once") is True and "token_hash" not in viewer_principal,
       "create response redacts stored token hash and marks raw token one-time")

    token_list = client.get("/api/access/tokens", params={"project": P})
    listed = token_list.json().get("tokens") or []
    serialized_list = str(token_list.json())
    ok(token_list.status_code == 200 and any(p["id"] == viewer_principal["id"] for p in listed),
       "admin can list scoped token principals")
    ok(viewer_token not in serialized_list and "token_hash" not in serialized_list,
       "token list never exposes raw tokens or token hashes")

    viewer_read = client.get("/api/board", params={"project": P}, headers=authz(viewer_token))
    ok(viewer_read.status_code == 200, "viewer token can read its project")
    viewer_write = client.post(
        f"/api/tasks?project={P}",
        json={"workstream_id": "ACCESS", "title": "viewer should not write"},
        headers=authz(viewer_token),
    )
    ok(viewer_write.status_code == 403, "viewer token cannot write tasks")
    viewer_admin = client.get(
        "/api/access/tokens", params={"project": P}, headers=authz(viewer_token))
    ok(viewer_admin.status_code == 403, "viewer token cannot list or mint credentials")

    contributor = client.post(
        "/api/access/tokens",
        params={"project": P},
        json={"kind": "agent", "display_name": "contributor adapter", "role": "contributor"},
    )
    contributor_token = contributor.json().get("token")
    ok(contributor.status_code == 200 and contributor_token,
       "admin can create contributor token from role preset")
    contributor_write = client.post(
        f"/api/tasks?project={P}",
        json={"workstream_id": "ACCESS", "title": "contributor token can write"},
        headers=authz(contributor_token),
    )
    ok(contributor_write.status_code == 200, "contributor token can write tasks")
    contributor_mint = client.post(
        "/api/access/tokens",
        params={"project": P},
        json={"kind": "agent", "display_name": "should fail", "role": "viewer"},
        headers=authz(contributor_token),
    )
    ok(contributor_mint.status_code == 403, "contributor token cannot mint credentials")

    bad_scope = client.post(
        "/api/access/tokens",
        params={"project": P},
        json={"kind": "agent", "display_name": "bad scope", "scopes": ["read", "write:root"]},
    )
    ok(bad_scope.status_code == 400, "unknown scopes fail closed")
    bad_kind = client.post(
        "/api/access/tokens",
        params={"project": P},
        json={"kind": "robot", "display_name": "bad kind", "role": "viewer"},
    )
    ok(bad_kind.status_code == 400, "unknown token principal kind fails closed")

    cross_project = client.get("/api/board", params={"project": "helm"}, headers=authz(viewer_token))
    ok(cross_project.status_code == 401, "project-scoped token cannot read another project")

    revoke = client.post(
        f"/api/access/tokens/{viewer_principal['id']}/revoke",
        params={"project": P},
    )
    ok(revoke.status_code == 200 and revoke.json()["revoked"],
       "admin can revoke a scoped token")
    revoked_read = client.get("/api/board", params={"project": P}, headers=authz(viewer_token))
    ok(revoked_read.status_code == 401, "revoked token is rejected immediately")
    include_revoked = client.get(
        "/api/access/tokens", params={"project": P, "include_revoked": True})
    revoked_rows = [
        p for p in include_revoked.json().get("tokens") or []
        if p["id"] == viewer_principal["id"]
    ]
    ok(bool(revoked_rows and revoked_rows[0].get("revoked_at")),
       "revoked token remains auditable when include_revoked is requested")

    with store._conn(P) as c:
        rows = c.execute(
            "SELECT kind, payload FROM activity WHERE kind IN "
            "('access.token_created', 'access.token_revoked')"
        ).fetchall()
    payload_text = "\n".join(row["payload"] for row in rows)
    ok(rows and viewer_token not in payload_text and "token_hash" not in payload_text,
       "token lifecycle activity is audited without leaking secrets")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
