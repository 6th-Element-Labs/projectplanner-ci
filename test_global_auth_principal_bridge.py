#!/usr/bin/env python3
"""Regression: under PM_GLOBAL_AUTH the middleware authenticates the caller and stashes
the principal on request.state; handlers' _principal() must trust it instead of
re-authenticating via the legacy bearer/per-project path (which 401s a global browser
login — the UI-7 agent-messaging bug: 'unauthorized: provide Authorization: Bearer …')."""
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="authbridge-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
try:
    import app as app_module  # noqa: E402
    from fastapi import HTTPException  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  auth bridge proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


_UNSET = object()


def _req(principal=_UNSET):
    st = types.SimpleNamespace()
    if principal is not _UNSET:
        st.principal = principal
    return types.SimpleNamespace(state=st)


try:
    store.init_db("switchboard")

    # Count legacy re-auth attempts so we can prove the middleware principal is trusted.
    calls = {"n": 0}
    real_auth = app_module.auth.authenticate_request

    def _counting_auth(*a, **k):
        calls["n"] += 1
        return {"id": "legacy-fallback", "kind": "system", "effective_scopes": ["admin"]}
    app_module.auth.authenticate_request = _counting_auth

    # 1. Browser principal (global-auth) with the required scope → trusted, no legacy re-auth.
    br = _req({"id": "user-1", "kind": "user", "effective_scopes": ["read", "write:tasks"]})
    p = app_module._principal(br, "switchboard", ("write:tasks",))
    ok(p.get("id") == "user-1" and calls["n"] == 0,
       "middleware-set browser principal is trusted without legacy re-authentication")

    # 2. Same caller, missing the required scope → 403 (still enforced), no legacy re-auth.
    ro = _req({"id": "user-2", "kind": "user", "effective_scopes": ["read"]})
    try:
        app_module._principal(ro, "switchboard", ("write:tasks",))
        ok(False, "insufficient scope should raise")
    except HTTPException as e:
        ok(e.status_code == 403 and calls["n"] == 0, "insufficient scope → 403, scopes still enforced")

    # 3. admin scope satisfies any requirement.
    ad = _req({"id": "root", "kind": "user", "effective_scopes": ["admin"]})
    p3 = app_module._principal(ad, "switchboard", ("write:system",))
    ok(p3.get("id") == "root" and calls["n"] == 0, "admin scope satisfies any requirement")

    # 4. No middleware principal (non-global-auth / dev / tests) → falls back to legacy auth.
    none_req = _req()  # state has no `principal` attribute at all
    p4 = app_module._principal(none_req, "switchboard", ("read",))
    ok(calls["n"] == 1 and p4.get("id") == "legacy-fallback",
       "no middleware principal → falls back to legacy authenticate_request")

    app_module.auth.authenticate_request = real_auth
except Exception:
    import traceback
    traceback.print_exc()
    failed += 1
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
