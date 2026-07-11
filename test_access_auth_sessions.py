#!/usr/bin/env python3
"""ACCESS-1 first-party auth/session regression."""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="access-auth-")
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
    from services.auth import session as auth_session  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  ACCESS auth/session smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

P = "switchboard"
EMAIL = "admin@test.com"
PASSWORD = "correct-horse-2026"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def title_exists(title):
    return any(t["title"] == title for t in store.list_tasks(project=P))


try:
    client = TestClient(app)
    auth_store.init()

    unauth = client.get("/api/board", params={"project": P})
    ok(unauth.status_code == 401, "required auth blocks unauthenticated project data reads")

    admin = auth_store.create_user(
        EMAIL, "Admin", auth.password_hash(PASSWORD), is_superadmin=True)
    store.ensure_bootstrap_project_owner(P, admin["id"], "admin", "Admin", actor="test")
    login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    ok(login.status_code == 200, "global admin login succeeds")
    ok(auth_session.COOKIE_NAME in client.cookies, "login issues an HttpOnly session cookie")

    ok(auth_store.create_user("other@test.com", "Other", auth.password_hash(PASSWORD)).get("id"),
       "second global user can be created without overwriting admin")

    session = client.get("/api/auth/session")
    ok(session.status_code == 200 and session.json()["user"]["id"] == admin["id"],
       "session cookie resolves the current user")

    board = client.get("/api/board", params={"project": P})
    ok(board.status_code == 200 and "workstreams" in board.json(),
       "session cookie can read project board data")

    title = "session-auth task create"
    created = client.post(f"/api/tasks?project={P}", json={"workstream_id": "ACCESS", "title": title})
    ok(created.status_code == 200 and created.json()["title"] == title,
       "session cookie can write through the web API")
    ok(title_exists(title), "session-auth write persisted")

    logout = client.post("/api/auth/logout")
    ok(logout.status_code == 200 and auth_session.COOKIE_NAME not in client.cookies,
       "logout clears the browser session cookie")
    ok(client.get("/api/board", params={"project": P}).status_code == 401,
       "logged-out session cannot read project data")

    ok(client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong-password"}).status_code == 401,
       "bad password is rejected")
    good_login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    ok(good_login.status_code == 200 and auth_session.COOKIE_NAME in client.cookies,
       "valid password login creates a new session")

    expired_token = auth.new_secret_token()
    auth_store.create_session(admin["id"], expired_token, 60)
    with auth_store._conn() as c:
        c.execute("UPDATE auth_sessions_v2 SET expires_at=? WHERE token_hash=?",
                  (time.time() - 1, auth_store.token_hash(expired_token)))
    expired_client = TestClient(app)
    expired_client.cookies.set(auth_session.COOKIE_NAME, expired_token)
    ok(expired_client.get("/api/board", params={"project": P}).status_code == 401,
       "expired session token is rejected")

    bearer_token = "adapter-bearer-token"
    store.create_principal(kind="agent", display_name="codex/access-bearer", token=bearer_token,
                           scopes=["read", "write:tasks"], project=P)
    ok(expired_client.get("/api/board", params={"project": P},
                          headers={"Authorization": f"Bearer {bearer_token}"}).status_code == 200,
       "adapter bearer tokens still authenticate API reads")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
