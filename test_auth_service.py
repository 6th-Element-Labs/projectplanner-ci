#!/usr/bin/env python3
"""Global auth router — register / login / session / deny-by-default access."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="auth-svc-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_TOP_LEVEL_PROJECTS"] = "alpha,beta"
os.environ["PM_JWT_SECRET"] = "test-secret-do-not-use-in-prod"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import store as auth_store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  auth service proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


def client():
    return TestClient(app)


try:
    ok(auth_store.__name__ == "switchboard.api.routers.auth.store",
       "auth persistence imports through the target Switchboard router package")
    store.init_project_registry()
    auth_store.init()
    for pid in ("alpha", "beta"):
        store.create_project(pid.title(), project_id=pid, actor="test")
        store.init_db(pid)

    # ---- self-service signup → deny-by-default ------------------------------
    c = client()
    r = c.post("/api/auth/register", json={"email": "New@Test.com", "display_name": "New", "password": "hunter2secret"})
    ok(r.status_code == 200, "register returns 200")
    new_user = r.json().get("user", {})
    ok(new_user.get("email") == "new@test.com", "email normalized to lowercase")
    ok(new_user.get("projects") == [], "brand-new user sees NO projects (deny-by-default)")
    ok("password_hash" not in new_user, "response never leaks password hash")
    new_id = new_user.get("id")

    ok(c.post("/api/auth/register", json={"email": "new@test.com", "display_name": "Dup", "password": "another8chars"}).status_code == 409,
       "duplicate email → 409")
    ok(c.post("/api/auth/register", json={"email": "x@y.com", "display_name": "Short", "password": "short"}).status_code == 422,
       "password < 8 chars → 422")

    # ---- login (email + password only, no project) --------------------------
    c2 = client()
    ok(c2.post("/api/auth/login", json={"email": "new@test.com", "password": "wrongpass1"}).status_code == 401,
       "wrong password → 401")
    lr = c2.post("/api/auth/login", json={"email": "new@test.com", "password": "hunter2secret"})
    ok(lr.status_code == 200, "login with correct password → 200")
    ok("taikun_session" in lr.cookies or any("taikun_session" in h for h in lr.headers.get("set-cookie", "").split(",")),
       "login sets taikun_session cookie")

    # ---- session reflects deny-by-default -----------------------------------
    s = c2.get("/api/auth/session")
    ok(s.status_code == 200 and s.json().get("authenticated"), "session validates the cookie")
    ok(s.json().get("user", {}).get("projects") == [], "logged-in new user still sees no projects")

    # ---- grant one project → user sees exactly that one ---------------------
    store.grant_project_role("alpha", "user", new_id, "admin", created_by="test",
                             scopes=["read", "write:tasks"])
    s2 = c2.get("/api/auth/session").json()
    got = sorted(p["id"] for p in s2.get("user", {}).get("projects", []))
    ok(got == ["alpha"], "granted user sees ONLY alpha, not beta")

    # ACCESS-17: PM_TOP_LEVEL_PROJECTS is a legacy built-in-home allowlist, not a
    # second access-control system. A newly created private project must appear in
    # both the refreshed session and the project-picker API for its owner even when
    # its id was not present in the process environment at boot.
    store.create_project("Gamma", project_id="gamma", owner_principal_id=new_id,
                         visibility="private", actor="test")
    s3 = c2.get("/api/auth/session").json()
    got3 = sorted(p["id"] for p in s3.get("user", {}).get("projects", []))
    ok(got3 == ["alpha", "gamma"],
       "new private project absent from PM_TOP_LEVEL_PROJECTS appears for its owner")
    picker = c2.get("/api/projects")
    picker_ids = sorted(p["id"] for p in picker.json().get("projects", []))
    ok(picker.status_code == 200 and picker_ids == ["alpha", "gamma"],
       "/api/projects picker immediately includes the newly created accessible project")

    # ---- superadmin sees everything -----------------------------------------
    admin = auth_store.create_user("boss@test.com", "Boss", __import__("auth").password_hash("bosspass99"), is_superadmin=True)
    ca = client()
    ca.post("/api/auth/login", json={"email": "boss@test.com", "password": "bosspass99"})
    allp = sorted(p["id"] for p in ca.get("/api/auth/session").json().get("user", {}).get("projects", []))
    ok(allp == ["alpha", "beta", "gamma"], "superadmin sees ALL projects")
    ok(ca.get("/api/auth/session").json().get("user", {}).get("is_superadmin") is True, "superadmin flag surfaced")

    # ---- middleware: the whole app honors the global session ----------------
    ok(c2.get("/api/board", params={"project": "alpha"}).status_code == 200,
       "granted user can READ their project via the app API")
    ok(c2.get("/api/board", params={"project": "beta"}).status_code == 403,
       "ungranted project is denied by the middleware (deny-by-default)")
    ok(sorted(p["id"] for p in c2.get("/api/projects").json().get("projects", [])) ==
       ["alpha", "gamma"],
       "/api/projects is filtered to the user's grants and owned projects")
    ok(ca.get("/api/board", params={"project": "beta"}).status_code == 200,
       "superadmin can read any project via the app API")
    ok(client().get("/api/board", params={"project": "alpha"}).status_code == 401,
       "no session → 401 on the app API")
    _tok = "agent-token-" + "z" * 24
    store.create_principal(kind="agent", display_name="bot", token=_tok, scopes=["read"], project="alpha")
    ok(client().get("/api/board", params={"project": "alpha"},
                    headers={"Authorization": "Bearer " + _tok}).status_code == 200,
       "bearer-token agents still authenticate under global auth (regression)")

    # ---- logout revokes the session -----------------------------------------
    c2.post("/api/auth/logout")
    ok(c2.get("/api/auth/session").status_code == 401, "session is dead after logout")

    # ---- no cookie → 401 ----------------------------------------------------
    ok(client().get("/api/auth/session").status_code == 401, "no cookie → 401")

    # ---- page routing (flag on) ---------------------------------------------
    pc = client()
    root_html = pc.get("/").text
    ok("Create an account" in root_html and 'id="email"' in root_html,
       "/ serves the global login page when unauthenticated")
    ok('id="project"' not in root_html, "global login page has NO project field")
    ok('id="signup-form"' in pc.get("/signup").text, "/signup serves the signup page")
    pc.post("/api/auth/register", json={"email": "pager@test.com", "display_name": "P", "password": "pagerpass1"})
    ok("app.js?v=" in pc.get("/").text, "/ serves the app once authenticated")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nauth service: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
