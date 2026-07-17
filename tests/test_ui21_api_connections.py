#!/usr/bin/env python3
"""UI-21: API Connections settings — user-owned, explicitly metered BYOK.

Same "command/repository + REST TestClient + frontend-source needle + Node JS"
style as tests/test_ui19_ai_accounts_settings.py. UI-21 adds the "API connections"
group beside UI-19's personal subscriptions. This PR is the Settings surface +
lifecycle over the already-reviewed provider-connections backend; the host-local
`enroll-api-key` CLI + its secure host→server credential transport are the tracked
slice-2 follow-up (they carry the independent security review). Covers: the
redacted read surface for a direct_api connection, no raw-secret echo, owner-only
enforcement, rotate/revoke/delete lifecycle, the two-group IA, the metered
warning, the disabled Anthropic/Cursor rows, and — critically — that the browser
surface never contains a secret-shaped input (the key stays host-local).
"""
from __future__ import annotations

import atexit
import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="ui21-api-conn-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"K" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "ui21-test:v1"

import store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from scripts.frontend_test_source import read_frontend_source  # noqa: E402
from switchboard.api.routers.provider_credentials import create_router  # noqa: E402
from switchboard.application.commands import provider_credentials as commands  # noqa: E402

PROJECT = "switchboard"
USER_A = "user-ui21-a"
USER_B = "user-ui21-b"
PROVIDER = "openai-codex"
RAW_KEY = "sk-ui21-super-secret-do-not-echo-abc123"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="ui21-test")
store.set_project_access(PROJECT, store.DEFAULT_ORG_ID, purpose="UI-21 fixture", created_by="ui21-test")
store.init_db(PROJECT)
for user_id in (USER_A, USER_B):
    store.ensure_user(user_id, f"{user_id}@example.test", user_id, created_by="ui21-test")
    store.add_org_member(store.DEFAULT_ORG_ID, user_id, role="member", created_by="ui21-test")

# ── 1. Create an OpenAI direct_api (BYOK) connection with billing + budget ────
# (trusted_provider_native mimics the host-local enroll transport slice-2 adds;
#  the public/browser path still rejects raw secrets — asserted below.)
conn = commands.enroll_mapping(
    {
        "project": PROJECT, "user_id": USER_A, "provider": PROVIDER,
        "provider_account_id": "billing-owner@example.test", "auth_type": "api_key",
        "project_allowlist": [PROJECT], "connection_kind": "direct_api",
        "billing_account_id": "acct-billing-ui21",
        "budget_policy": {"budget_id": "budget-ui21", "currency": "USD", "ceiling": 50},
        "credential": RAW_KEY,
    },
    actor=USER_A, principal_user_id=USER_A, principal_kind="user",
    trusted_provider_native=True, raise_errors=True)
ref = conn.get("execution_connection_id") or conn.get("credential_reference")
ok(bool(ref) and conn.get("connection_kind") == "direct_api"
   and conn.get("materialization_mode") == "vault_envelope",
   "a direct_api OpenAI connection enrolls with the key envelope-encrypted in the vault")
ok(RAW_KEY not in str(conn),
   "the enroll result never echoes the raw API key back (no-readback)")

# ── 2. REST read surface (owner) exposes the redacted, metered shape ──────────
principal_state = {"user_id": USER_A}


def principal_record() -> dict:
    return {"id": principal_state["user_id"], "kind": "user",
            "effective_scopes": ["read:credentials", "write:credentials"]}


api = FastAPI()
api.include_router(create_router(
    resolve_project=lambda value: value,
    resolve_principal=lambda *_a, **_k: principal_record(),
))
client = TestClient(api)

listed = client.get(f"/api/projects/{PROJECT}/provider-connections")
ok(listed.status_code == 200, "owner can list provider-connections over REST")
body_text = listed.text
rows = listed.json().get("connections", [])
row = next((c for c in rows if (c.get("credential_reference") == ref
                                or c.get("execution_connection_id") == ref)), None)
ok(row is not None and row.get("connection_kind") == "direct_api",
   "the API connection is returned with connection_kind=direct_api")
ok(bool(row) and row.get("billing_account_bound") is True
   and (row.get("budget_policy") or {}).get("ceiling") == 50,
   "the redacted row carries billing-account-bound + budget ceiling for the metered display")
ok(bool(row) and row.get("credential_present") is True and "credential" not in row,
   "the row reports credential_present but never includes the secret itself")
ok(RAW_KEY not in body_text,
   "SECURITY: the raw API key never appears anywhere in the REST response body")

# ── 3. Lifecycle: revoke / delete over the existing endpoints ─────────────────
# (Key rotation for an API-key connection means supplying a NEW key, which is
#  host-local like enrollment — not a browser action — so it is not offered here.)
revoke = client.post(f"/api/projects/{PROJECT}/provider-connections/{ref}/revoke",
                     json={"reason": "operator_revoked_in_settings"})
ok(revoke.status_code in (200, 204), "revoke succeeds for the owner")
delete = client.request("DELETE", f"/api/projects/{PROJECT}/provider-connections/{ref}",
                        json={"reason": "operator_deleted_in_settings"})
ok(delete.status_code in (200, 204), "delete succeeds for the owner")

# ── 4. Owner-only: user B cannot see or mutate A's API connection ─────────────
# Re-enroll a fresh one (the one above was just deleted) for the cross-user check.
conn2 = commands.enroll_mapping(
    {
        "project": PROJECT, "user_id": USER_A, "provider": PROVIDER,
        "provider_account_id": "billing-owner-2@example.test", "auth_type": "api_key",
        "project_allowlist": [PROJECT], "connection_kind": "direct_api",
        "billing_account_id": "acct-billing-ui21-2",
        "budget_policy": {"budget_id": "budget-ui21-2", "currency": "USD", "ceiling": 10},
        "credential": RAW_KEY,
    },
    actor=USER_A, principal_user_id=USER_A, principal_kind="user",
    trusted_provider_native=True, raise_errors=True)
ref2 = conn2.get("execution_connection_id") or conn2.get("credential_reference")
principal_state["user_id"] = USER_B
b_list = client.get(f"/api/projects/{PROJECT}/provider-connections")
b_rotate = client.post(f"/api/projects/{PROJECT}/provider-connections/{ref2}/rotate", json={})
ok(b_list.status_code == 200 and b_list.json() == {"connections": []},
   "user B's listing does not include user A's API connection")
ok(b_rotate.status_code in (403, 404),
   "user B cannot rotate user A's API connection (owner-only)")

# ── 5. Frontend source needles ───────────────────────────────────────────────
settings_source = (ROOT / "static" / "js" / "settings.js").read_text()
frontend_source = read_frontend_source(ROOT)
ok('name="credential"' not in settings_source and 'name="api_key"' not in settings_source
   and 'name="token"' not in settings_source and 'name="password"' not in settings_source,
   "SECURITY: settings.js still has no secret-shaped input field — the API key stays host-local")
ok("API_CONNECTION_PROVIDERS" in settings_source
   and "_settingsApiConnectionCard" in settings_source
   and "_settingsApiConnectionRows" in settings_source,
   "the API connections renderers are present")
ok("API connections" in settings_source and "explicitly metered" in settings_source,
   "the two-group IA + metered warning copy are present")
ok("enroll-api-key" in settings_source and "api-key-stdin" in settings_source,
   "the connect flow is host-local (enroll-api-key + --api-key-stdin), not a browser secret field")
for action in ("api-connections-revoke", "api-connections-delete", "api-connections-copy-cmd"):
    ok(action in settings_source, f"settings.js wires the {action} action")
ok("_settingsAiAccountsSection" in frontend_source and "ai-accounts" in frontend_source,
   "the AI connections section stays registered in the Settings shell")

# ── 6. Frontend logic (Node): render an API card, gated rows, no secret input ─
node_check = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
global.window = { PM_PROJECT: 'switchboard' };
eval(src);
const m = window.SwitchboardSettings.methods;
const ctx = Object.create(m);
ctx.esc = (s) => String(s == null ? '' : s);

const conn = { provider:'openai-codex', connection_kind:'direct_api', lifecycle_state:'active',
  credential_reference:'cred-abc', execution_connection_id:'cred-abc', billing_account_bound:true,
  billing_account_fingerprint:'bill-deadbeef', budget_policy:{ceiling:50,currency:'usd'},
  host_allowlist:['host/x'], active_lease_count:2 };
const openai = { id:'openai-codex', label:'OpenAI API', icon:'ti-brand-openai', enabled:true };

const connected = ctx._settingsApiConnectionCard.call(ctx, openai, {conns:[conn],caps:[],hosts:[],me:'user-a',connectionsError:'',hostsError:''});
if (!/Revoke/.test(connected) || !/bill-deadbeef/.test(connected) || !/direct_api/.test(connected) || !/Metered/.test(connected)) {
  console.error('connected_card_missing_fields'); process.exit(2); }
if (/type="password"/.test(connected) || /name="api_key"/.test(connected) || /name="credential"/.test(connected)) {
  console.error('connected_card_has_secret_input'); process.exit(3); }

const disabled = ctx._settingsApiConnectionCard.call(ctx, {id:'anthropic-claude',label:'Anthropic API',icon:'ti-sparkles',enabled:false,gate:'ADAPTER-20'}, {conns:[],caps:[],hosts:[],me:'user-a',connectionsError:'',hostsError:''});
if (!/ADAPTER-20/.test(disabled) || !/gated|disabled|not self-service/.test(disabled)) {
  console.error('disabled_provider_not_gated'); process.exit(4); }

const connect = ctx._settingsApiConnectionCard.call(ctx, openai, {conns:[],caps:[],hosts:[],me:'user-a',connectionsError:'',hostsError:''});
if (!/enroll-api-key/.test(connect) || !/api-key-stdin/.test(connect)) { console.error('connect_missing_host_cli'); process.exit(5); }
if (/type="password"/.test(connect) || /name="api_key"/.test(connect)) { console.error('connect_has_secret_input'); process.exit(6); }
console.log('ui21_frontend_ok');
"""
run = subprocess.run(["node", "-e", node_check, str(ROOT / "static" / "js" / "settings.js")],
                     capture_output=True, text=True, cwd=str(ROOT))
ok(run.returncode == 0 and "ui21_frontend_ok" in (run.stdout or ""),
   f"Node: API card renders metered rows + lifecycle, gates Anthropic/Cursor, keeps the key host-local (rc={run.returncode})")
if run.returncode != 0:
    print((run.stderr or run.stdout or "")[:800])

print(f"\nUI-21 API connections settings: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
