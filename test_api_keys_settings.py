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
from scripts.frontend_test_source import read_frontend_source

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

    # ---- index.html shell (UI-20 2/6: the modal + rail button are retired) ---
    index = client.get("/")
    ok(index.status_code == 200 and 'id="settings-panel"' in index.text,
       "index.html hosts the unified Settings shell panel")
    for gone in ('id="apikeys-modal"', 'id="btn-project-apikeys"'):
        ok(gone not in index.text, f"legacy {gone} retired from index.html")

    # ---- frontend wiring (the surface is now inline in the Settings shell) ---
    app_js = read_frontend_source(os.path.dirname(__file__))
    for needle in (
        "_settingsTokensSection",
        "_settingsTokensTableHtml",
        "_settingsCreateToken",
        "_settingsRevokeToken",
        "_settingsTokensReload",
    ):
        ok(needle in app_js, f"settings.js defines {needle}")
    # Relabelled so control-plane tokens are not confused with model-provider API keys.
    ok("Switchboard access tokens" in app_js,
       "tokens section is relabelled 'Switchboard access tokens'")
    # The least-privilege scope picker and shown-once banner render inline, off the
    # write:system-gated section (settings.js gates 'tokens' at write:system).
    ok('id="apikeys-scopes"' in app_js and 'id="apikeys-new-banner"' in app_js,
       "the create form scope picker + shown-once banner render inline in Settings")
    ok("_sSend(`api/access/tokens" in app_js and "settings-token-scope" in app_js,
       "create/revoke run through the admin-gated _sSend path (403 -> write:system error)")
    # The shown-once wipe is re-anchored off the modal's hidden.bs.modal onto the panel swap.
    ok("_clearApiKeySecret" in app_js and 'id="apikeys-modal"' not in app_js,
       "shown-once token wipe survives; the modal wiring is gone")
    ok("wipe shown-once token on panel swap" in app_js,
       "the wipe fires on the _settingsSelect panel swap, not modal close")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nAPI keys settings proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
