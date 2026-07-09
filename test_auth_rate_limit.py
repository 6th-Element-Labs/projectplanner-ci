#!/usr/bin/env python3
"""HARDEN-45 — brute-force throttle + login audit trail on the global auth service.

Proves: N failed logins lock the account/IP (even against the correct password),
a couple of typos followed by the right password still succeed, the reset endpoints
are rate-limited, every attempt is written to the audit trail, and the whole thing
can be switched off with PM_AUTH_RATELIMIT=0.
"""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="auth-ratelimit-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_TOP_LEVEL_PROJECTS"] = "alpha"
os.environ["PM_GLOBAL_AUTH"] = "1"
os.environ["PM_JWT_SECRET"] = "test-secret-do-not-use-in-prod"
# Low thresholds + a wide window so nothing ages out mid-test.
os.environ["PM_AUTH_LOGIN_MAX_FAILURES"] = "3"
os.environ["PM_AUTH_LOGIN_WINDOW_SECONDS"] = "300"
os.environ["PM_AUTH_RESET_MAX_REQUESTS"] = "3"
os.environ["PM_AUTH_RESET_WINDOW_SECONDS"] = "300"
os.environ["PM_AUTH_RESET_CONSUME_MAX_FAILURES"] = "3"
os.environ["PM_AUTH_RESET_CONSUME_WINDOW_SECONDS"] = "300"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import store  # noqa: E402
from services.auth import store as auth_store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  auth rate-limit proof requires optional dependency: {exc.name}")
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


EMAIL = "rl@test.com"
PW = "correct-horse-99"


def do_login(email, password):
    return client().post("/api/auth/login", json={"email": email, "password": password})


try:
    store.init_project_registry()
    auth_store.init()
    store.create_project("Alpha", project_id="alpha", actor="test")
    store.init_db("alpha")
    auth_store.create_user(EMAIL, "RL", auth.password_hash(PW))

    # ---- legitimate login is unaffected -------------------------------------
    ok(do_login(EMAIL, PW).status_code == 200, "correct login succeeds (baseline)")
    ok(do_login(EMAIL, PW).status_code == 200, "repeated correct logins are never throttled")

    # a couple of typos, then the right password, still works (streak resets on success)
    ok(do_login(EMAIL, "wrong-1").status_code == 401, "typo #1 -> 401")
    ok(do_login(EMAIL, "wrong-2").status_code == 401, "typo #2 -> 401")
    ok(do_login(EMAIL, PW).status_code == 200, "correct password after a few typos still succeeds")

    # ---- N failures lock the account ----------------------------------------
    ok(do_login(EMAIL, "bad-a").status_code == 401, "failure #1 -> 401")
    ok(do_login(EMAIL, "bad-b").status_code == 401, "failure #2 -> 401")
    ok(do_login(EMAIL, "bad-c").status_code == 401, "failure #3 -> 401")

    locked = do_login(EMAIL, "bad-d")
    ok(locked.status_code == 429, "the attempt past the threshold is locked out (429)")
    ra = locked.headers.get("retry-after")
    ok(ra is not None and ra.isdigit() and int(ra) > 0,
       f"locked response carries a positive Retry-After header (got {ra!r})")

    # the lockout ignores credentials — even the RIGHT password is refused while locked
    ok(do_login(EMAIL, PW).status_code == 429,
       "correct password is still refused (429) while the account is locked")

    # ---- the audit trail recorded everything --------------------------------
    events = auth_store.recent_auth_events("login")
    outcomes = {e["outcome"] for e in events}
    ok("failure" in outcomes, "failed logins are recorded in the audit trail")
    ok("success" in outcomes, "successful logins are recorded in the audit trail")
    ok("throttled" in outcomes, "throttled attempts are recorded in the audit trail")
    ok(any(e["outcome"] == "failure" and e["email"] == EMAIL for e in events),
       "failure rows capture the attempted email")

    # ---- password-reset request endpoint is throttled -----------------------
    fp = lambda em: client().post("/api/auth/forgot-password", json={"email": em})
    r0 = fp("nobody@nope.com")
    ok(r0.status_code == 200 and "If an account exists" in r0.json().get("message", ""),
       "forgot-password stays 200 + generic for an unknown email (anti-enumeration)")
    ok(fp(EMAIL).status_code == 200, "forgot-password #2 -> 200")
    ok(fp(EMAIL).status_code == 200, "forgot-password #3 -> 200")
    fp_locked = fp(EMAIL)
    ok(fp_locked.status_code == 429, "forgot-password past the limit is throttled (429)")
    ok((fp_locked.headers.get("retry-after") or "").isdigit(),
       "throttled forgot-password carries a Retry-After header")

    # ---- reset-password (token spend) is throttled per IP -------------------
    rp = lambda: client().post("/api/auth/reset-password",
                               json={"token": "bogus-token-value", "new_password": "brandnewpw1"})
    ok(rp().status_code == 400, "reset with a bad token -> 400 (#1)")
    ok(rp().status_code == 400, "reset with a bad token -> 400 (#2)")
    ok(rp().status_code == 400, "reset with a bad token -> 400 (#3)")
    ok(rp().status_code == 429, "reset-password past the limit is throttled (429)")

    # ---- kill switch: PM_AUTH_RATELIMIT=0 disables all of it -----------------
    os.environ["PM_AUTH_RATELIMIT"] = "0"
    auth_store.create_user("off@test.com", "Off", auth.password_hash("offpassword1"))
    codes = {do_login("off@test.com", "nope-" + str(i)).status_code for i in range(6)}
    ok(codes == {401}, "with the kill switch on, repeated failures never lock (all 401, no 429)")
    os.environ["PM_AUTH_RATELIMIT"] = "1"

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nauth rate-limit: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
