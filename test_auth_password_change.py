#!/usr/bin/env python3
"""Self-service password change — service logic + the /api/auth/change-password route.

Proves a signed-in user can change their own password: current-password required,
min length + no-op enforced, old password stops working, new one works, and OTHER
sessions are revoked while the caller stays signed in.
"""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="auth-pwchange-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_TOP_LEVEL_PROJECTS"] = "demo"
os.environ["PM_JWT_SECRET"] = "test-secret"
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import service as asvc  # noqa: E402
from switchboard.api.routers.auth import store as ast  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    ast.init()
    ast.create_user("steve@example.com", "Steve", auth.password_hash("origPass123"), is_superadmin=True)

    _, token, _ = asvc.login("steve@example.com", "origPass123")
    ok(bool(token), "login issues a session token")

    # a second device / session
    _, token2, _ = asvc.login("steve@example.com", "origPass123")
    ok(asvc.current_user(token2) is not None, "second session valid before change")

    # guardrails
    try:
        asvc.change_password(token, "WRONGcurrent", "brandNew456"); ok(False, "wrong current rejected")
    except asvc.AuthError as e:
        ok(e.status == 403, "wrong current password -> 403")
    try:
        asvc.change_password(token, "origPass123", "short"); ok(False, "short rejected")
    except asvc.AuthError as e:
        ok(e.status == 422, "new password < 8 chars -> 422")
    try:
        asvc.change_password(token, "origPass123", "origPass123"); ok(False, "no-op rejected")
    except asvc.AuthError as e:
        ok(e.status == 422, "unchanged password -> 422")
    try:
        asvc.change_password("not-a-real-token", "origPass123", "brandNew456"); ok(False, "no session rejected")
    except asvc.AuthError as e:
        ok(e.status == 401, "no valid session -> 401")

    # the happy path
    res = asvc.change_password(token, "origPass123", "brandNew456")
    ok(res.get("email") == "steve@example.com" and "password_hash" not in res,
       "change succeeds and never leaks the hash")

    try:
        asvc.login("steve@example.com", "origPass123"); ok(False, "old password refused")
    except asvc.AuthError as e:
        ok(e.status == 401, "old password no longer works")
    ok(asvc.login("steve@example.com", "brandNew456")[1], "new password works")

    ok(asvc.current_user(token) is not None, "caller's own session survives the change")
    ok(asvc.current_user(token2) is None, "other sessions are revoked on change")

    # ---- HTTP route ---------------------------------------------------------
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
    client = TestClient(app)

    r = client.post("/api/auth/register",
                    json={"email": "carol@example.com", "display_name": "Carol", "password": "carolPass1"})
    ok(r.status_code == 200, "register via API -> 200 (sets cookie)")

    anon = TestClient(app)
    r0 = anon.post("/api/auth/change-password",
                   json={"current_password": "x", "new_password": "whatever12"})
    ok(r0.status_code == 401, "change-password without a session -> 401")

    r1 = client.post("/api/auth/change-password",
                     json={"current_password": "wrong", "new_password": "newCarol12"})
    ok(r1.status_code == 403, "change-password with wrong current -> 403")

    r2 = client.post("/api/auth/change-password",
                     json={"current_password": "carolPass1", "new_password": "newCarol12"})
    ok(r2.status_code == 200, "change-password success -> 200")

    r3 = TestClient(app).post("/api/auth/login",
                              json={"email": "carol@example.com", "password": "newCarol12"})
    ok(r3.status_code == 200, "new password logs in via API")

    r4 = TestClient(app).post("/api/auth/login",
                              json={"email": "carol@example.com", "password": "carolPass1"})
    ok(r4.status_code == 401, "old password rejected via API")

    ok(client.get("/account").status_code == 200, "GET /account serves the settings page")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nauth password change: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
