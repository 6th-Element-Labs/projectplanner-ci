#!/usr/bin/env python3
"""Auth migration — per-project password logins become global email+password logins.

Proves the one-shot `scripts/migrate_auth_to_global.py` lets an existing per-project
credential sign in at the GLOBAL login with the SAME password, covering both the
pre-existing-users-row path (prod's steve/root) and the fresh-row path, plus
owner->superadmin, wrong-password rejection, and idempotent re-runs.
"""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="auth-migration-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_TOP_LEVEL_PROJECTS"] = "demo"
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

import auth  # noqa: E402
import store  # noqa: E402
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import service as asvc  # noqa: E402
from switchboard.api.routers.auth import store as ast  # noqa: E402
import migrate_auth_to_global as M  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    ast.init()
    store.create_project("Demo", project_id="demo", actor="test")
    store.init_db("demo")

    # alice OWNS the project -> migration should mark her superadmin.
    with store._registry_conn() as c:
        c.execute("INSERT INTO project_access(project_id,org_id,owner_user_id,created_at,updated_at) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET owner_user_id=excluded.owner_user_id",
                  ("demo", "org-x", "user-alice", time.time(), time.time()))
        # bob mirrors prod: a users row already exists but WITHOUT an email yet.
        c.execute("INSERT INTO users(id,email,display_name,created_at,disabled_at) VALUES (?,?,?,?,NULL)",
                  ("user-bob", None, "Bob Builder", time.time()))

    # per-project password logins (exactly like prod's 'steve'/'root')
    store.create_password_principal(login="alice", display_name="Alice",
        password_hash=auth.password_hash("alicePass123"), scopes=["read", "write:tasks"],
        principal_id="user-alice", project="demo")
    store.create_password_principal(login="bob", display_name="Bob Builder",
        password_hash=auth.password_hash("bobPass456"), scopes=["read"],
        principal_id="user-bob", project="demo")

    migrated = M.migrate()
    by_login = {m["login"]: m for m in migrated}
    ok(set(by_login) == {"alice", "bob"}, "both per-project logins migrated")
    ok(by_login["alice"]["email"] == "alice@taikunai.com", "alice mapped to <login>@taikunai.com")
    ok(by_login["alice"]["superadmin"] is True, "project owner (alice) marked superadmin")
    ok(by_login["bob"]["superadmin"] is False, "non-owner (bob) is NOT superadmin")

    # THE proof: fresh-row path — global login with alice's ORIGINAL per-project password
    user, token, exp = asvc.login("alice@taikunai.com", "alicePass123")
    ok(bool(token) and exp > time.time(), "alice logs in globally with her per-project password")
    ok(user.get("is_superadmin") is True, "alice's session is superadmin")
    ok(any(p.get("id") == "demo" for p in user.get("projects", [])), "superadmin alice sees the project")

    # pre-existing-row path — bob's users row had no email; migration filled it, login works
    bob_user, bob_tok, _ = asvc.login("bob@taikunai.com", "bobPass456")
    ok(bool(bob_tok), "bob (pre-existing users row, email backfilled) logs in globally")
    ok(bob_user.get("is_superadmin") is False, "bob is a normal user")
    ok(bob_user.get("projects", []) == [], "deny-by-default: bob (no grants) sees no projects")

    # wrong password must be rejected
    try:
        asvc.login("alice@taikunai.com", "wrongpass000")
        ok(False, "wrong password rejected")
    except asvc.AuthError as e:
        ok(e.status == 401, "wrong password rejected with 401")

    # idempotent: a second run changes nothing and login still works
    again = M.migrate()
    ok(len(again) == 2, "re-run migrates the same 2 (idempotent)")
    user2, tok2, _ = asvc.login("alice@taikunai.com", "alicePass123")
    ok(bool(tok2), "login still works after idempotent re-run")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nauth migration: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
