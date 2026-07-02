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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient  # noqa: E402
    import auth  # noqa: E402
    import store  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  ACCESS auth/session smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "switchboard"
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

    unauth = client.get("/api/board", params={"project": P})
    ok(unauth.status_code == 401, "required auth blocks unauthenticated project data reads")

    bootstrap = client.post(
        "/api/auth/bootstrap",
        json={"project": P, "login": "admin", "password": PASSWORD},
    )
    ok(bootstrap.status_code == 200, "local first-admin bootstrap succeeds")
    ok(auth.session_cookie_name() in client.cookies, "bootstrap issues an HttpOnly session cookie")

    duplicate = client.post(
        "/api/auth/bootstrap",
        json={"project": P, "login": "admin2", "password": PASSWORD},
    )
    ok(duplicate.status_code == 409, "bootstrap refuses to overwrite an existing password admin")

    me = client.get("/api/auth/me", params={"project": P})
    ok(me.status_code == 200 and me.json()["principal"]["id"].startswith("user-"),
       "session cookie resolves the current principal")

    board = client.get("/api/board", params={"project": P})
    ok(board.status_code == 200 and "workstreams" in board.json(),
       "session cookie can read project board data")

    title = "session-auth task create"
    created = client.post(
        f"/api/tasks?project={P}",
        json={"workstream_id": "ACCESS", "title": title},
    )
    ok(created.status_code == 200 and created.json()["title"] == title,
       "session cookie can write through the web API")
    ok(title_exists(title), "session-auth write persisted")

    logout = client.post("/api/auth/logout", json={"project": P})
    ok(logout.status_code == 200 and auth.session_cookie_name() not in client.cookies,
       "logout clears the browser session cookie")
    after_logout = client.get("/api/board", params={"project": P})
    ok(after_logout.status_code == 401, "logged-out session cannot read project data")

    bad_login = client.post(
        "/api/auth/login",
        json={"project": P, "login": "admin", "password": "wrong-password"},
    )
    ok(bad_login.status_code == 401, "bad password is rejected")
    good_login = client.post(
        "/api/auth/login",
        json={"project": P, "login": "admin", "password": PASSWORD},
    )
    ok(good_login.status_code == 200 and auth.session_cookie_name() in client.cookies,
       "valid password login creates a new session")

    principal_id = good_login.json()["principal"]["id"]
    expired_token = auth.new_secret_token()
    expired = store.create_auth_session(principal_id, expired_token, 60, project=P)
    with store._conn(P) as c:
        c.execute("UPDATE auth_sessions SET expires_at=? WHERE session_id=?",
                  (time.time() - 1, expired["session_id"]))
    expired_client = TestClient(app)
    expired_client.cookies.set(auth.session_cookie_name(), expired_token)
    expired_read = expired_client.get("/api/board", params={"project": P})
    ok(expired_read.status_code == 401, "expired session token is rejected")

    bearer_token = "adapter-bearer-token"
    store.create_principal(
        kind="agent",
        display_name="codex/access-bearer",
        token=bearer_token,
        scopes=["read", "write:tasks"],
        project=P,
    )
    bearer_read = expired_client.get(
        "/api/board",
        params={"project": P},
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    ok(bearer_read.status_code == 200, "adapter bearer tokens still authenticate API reads")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
