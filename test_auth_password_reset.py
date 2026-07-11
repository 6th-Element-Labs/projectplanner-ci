#!/usr/bin/env python3
"""Forgot-password flow — emailed single-use reset link -> set a new password.

Covers the service + the /api/auth/forgot-password and /api/auth/reset-password
routes: a link is issued only for real accounts (anti-enumeration), the token is
single-use and expiring, a weak password is refused without burning the token, and
completing a reset signs out existing sessions. The email transport is stubbed so
the raw token can be captured (prod sends it via notify/SMTP).
"""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="auth-pwreset-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_TOP_LEVEL_PROJECTS"] = "demo"
os.environ["PM_JWT_SECRET"] = "test-secret"
os.environ["PM_AUTH_MODE"] = "dev-open"
for _k in ("PM_SMTP_HOST", "PM_SMTP_USER", "PM_SMTP_PASSWORD"):
    os.environ.pop(_k, None)  # keep email in dry-run; we capture via the stub below
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import store  # noqa: E402
import scripts.switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import service as asvc  # noqa: E402
from switchboard.api.routers.auth import store as ast  # noqa: E402

# capture emailed reset URLs instead of sending
_sent = []
asvc._send_reset_email = lambda to, url: _sent.append((to, url))

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def token_of(url):
    return url.split("token=", 1)[1]


try:
    ast.init()
    ast.create_user("user@example.com", "User", auth.password_hash("origPass123"))

    # request a reset for a real account -> a link is emailed
    _sent.clear()
    asvc.request_password_reset("user@example.com", "https://plan.taikunai.com")
    ok(len(_sent) == 1, "reset link issued for a real account")
    to, url = _sent[0]
    ok(to == "user@example.com" and "/reset-password?token=" in url, "email carries the reset link")
    tok = token_of(url)

    # anti-enumeration: unknown email -> nothing sent (endpoint still 200 neutral)
    _sent.clear()
    asvc.request_password_reset("ghost@example.com", "https://plan.taikunai.com")
    ok(len(_sent) == 0, "no link for a non-account (no user enumeration)")

    # weak password refused, and it does NOT burn the token
    try:
        asvc.reset_password(tok, "short"); ok(False, "weak refused")
    except asvc.AuthError as e:
        ok(e.status == 422, "weak new password -> 422")

    # a live session, to prove reset revokes it
    _, sess_tok, _ = asvc.login("user@example.com", "origPass123")
    ok(asvc.current_user(sess_tok) is not None, "session valid before reset")

    # happy path — same (unburned) token now works
    asvc.reset_password(tok, "brandNew456")
    ok(asvc.login("user@example.com", "brandNew456")[1], "new password works after reset")
    try:
        asvc.login("user@example.com", "origPass123"); ok(False, "old refused")
    except asvc.AuthError as e:
        ok(e.status == 401, "old password rejected after reset")
    ok(asvc.current_user(sess_tok) is None, "reset signs out existing sessions")

    # single-use + invalid token
    try:
        asvc.reset_password(tok, "another999"); ok(False, "reuse refused")
    except asvc.AuthError as e:
        ok(e.status == 400, "used token rejected (single-use)")
    try:
        asvc.reset_password("not-a-real-token", "another999"); ok(False, "garbage refused")
    except asvc.AuthError as e:
        ok(e.status == 400, "invalid token -> 400")

    # expired token (store level)
    uid = ast.get_user_by_email("user@example.com")["id"]
    raw = "to-be-expired-xyz"
    ast.create_reset_token(uid, raw, 3600)
    with store._registry_conn() as c:
        c.execute("UPDATE password_resets SET expires_at=? WHERE token_hash=?",
                  (time.time() - 1, ast.token_hash(raw)))
    ok(ast.consume_reset_token(raw) is None, "expired token is rejected")

    # ---- HTTP routes --------------------------------------------------------
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
    client = TestClient(app)
    client.post("/api/auth/register",
                json={"email": "carol@example.com", "display_name": "Carol", "password": "carolPass1"})

    _sent.clear()
    r = client.post("/api/auth/forgot-password", json={"email": "carol@example.com"})
    ok(r.status_code == 200 and "reset link" in (r.json().get("message", "").lower()),
       "POST /forgot-password -> 200 neutral message")
    ok(len(_sent) == 1, "forgot-password issued a link for the real account")
    ctok = token_of(_sent[0][1])

    _sent.clear()
    r2 = client.post("/api/auth/forgot-password", json={"email": "ghost@example.com"})
    ok(r2.status_code == 200 and len(_sent) == 0, "forgot-password neutral + silent for unknown email")

    r3 = client.post("/api/auth/reset-password", json={"token": ctok, "new_password": "newCarol12"})
    ok(r3.status_code == 200, "POST /reset-password with a valid token -> 200")
    ok(TestClient(app).post("/api/auth/login",
       json={"email": "carol@example.com", "password": "newCarol12"}).status_code == 200,
       "new password logs in via API")

    r4 = client.post("/api/auth/reset-password", json={"token": "nope", "new_password": "whatever12"})
    ok(r4.status_code == 400, "reset-password with bad token -> 400")

    ok(client.get("/forgot-password").status_code == 200, "GET /forgot-password serves")
    ok(client.get("/reset-password?token=x").status_code == 200, "GET /reset-password serves")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nauth password reset: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
