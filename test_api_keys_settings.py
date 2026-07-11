#!/usr/bin/env python3
"""UI-4: prove the Settings -> API keys screen REST contract + UI wiring.

Self-serve scoped tokens over /api/access/tokens: mint with chosen scopes, show the raw
key once, list active keys, revoke. Admin-gated (write:system). Same "API + app.js-needle"
shape as the other operator-UI proofs.
"""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="api-keys-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  API keys proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

P = "qa-apikeys"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


client = TestClient(app)

try:
    store.init_project_registry()
    store.create_project("API keys QA", project_id=P, actor="test")
    store.init_db(P)

    # ---- List: scopes + kinds vocabulary for the create modal ---------------
    listed = client.get("/api/access/tokens", params={"project": P})
    ok(listed.status_code == 200, "GET /api/access/tokens returns 200")
    body = listed.json()
    ok(isinstance(body.get("scope_definitions"), dict) and "admin" in body["scope_definitions"],
       "list returns scope_definitions (role -> scopes) for the modal")
    ok("agent" in (body.get("valid_kinds") or []),
       "list returns valid_kinds for the kind picker")
    ok(isinstance(body.get("tokens"), list),
       "list returns the tokens array for the table")

    # ---- Create: mint a key, shown once -------------------------------------
    created = client.post("/api/access/tokens", params={"project": P},
                          json={"display_name": "ci-mirror", "kind": "agent",
                                "scopes": ["read", "write:ixp"]})
    ok(created.status_code == 200, "POST /api/access/tokens returns 200")
    cb = created.json()
    ok(bool(cb.get("token")) and cb.get("token_returned_once") is True,
       "create returns the raw token exactly once (shown-once banner)")
    pid = (cb.get("principal") or {}).get("id")
    ok(bool(pid), "create returns the new principal id")
    ok(set(["read", "write:ixp"]).issubset(set((cb.get("principal") or {}).get("scopes") or [])),
       "created key carries the chosen least-privilege scopes")

    # ---- List reflects the new key ------------------------------------------
    after = client.get("/api/access/tokens", params={"project": P}).json()
    mine = next((t for t in (after.get("tokens") or []) if t.get("id") == pid), None)
    ok(mine is not None and mine.get("display_name") == "ci-mirror",
       "new key appears in the list with its name")
    ok("token" not in (mine or {}),
       "listed keys never re-expose the raw token (hash-only storage)")

    # ---- Revoke -------------------------------------------------------------
    revoked = client.post(f"/api/access/tokens/{pid}/revoke", params={"project": P}, json={})
    ok(revoked.status_code == 200, "POST /api/access/tokens/{id}/revoke returns 200")
    active = client.get("/api/access/tokens", params={"project": P}).json()
    ok(not any(t.get("id") == pid for t in (active.get("tokens") or [])),
       "revoked key drops out of the active list")

    # ---- index.html shell ----------------------------------------------------
    index = client.get("/")
    for needle in ('id="apikeys-modal"', 'id="btn-project-apikeys"', 'id="apikeys-scopes"',
                   'id="apikeys-new-banner"'):
        ok(index.status_code == 200 and needle in index.text,
           f"index.html exposes {needle}")

    # ---- app.js wiring -------------------------------------------------------
    app_js = open(os.path.join(os.path.dirname(__file__), "static", "app.js"),
                  encoding="utf-8").read()
    for needle in (
        "openApiKeys",
        "_loadApiKeys",
        "_renderApiKeyForm",
        "_renderApiKeysTable",
        "_createApiKey",
        "_revokeApiKey",
    ):
        ok(needle in app_js, f"app.js defines {needle}")
    ok("apikeys-admin-warn" in app_js and "403" in app_js,
       "non-admin load is handled with a read-only notice")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nAPI keys settings proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
