#!/usr/bin/env python3
"""UI-19: Personal AI Accounts settings — provider-native enroll/verify/bind/revoke.

Same "API + served-HTML + frontend-source needle" style as test_ui18_settings_shell.py
and tests/test_enforce8_provider_ownership.py: no browser, no Playwright — assert on
(a) the application-command/repository contract directly, (b) the REST surface via
FastAPI's TestClient, and (c) needles in the served frontend source. Covers this task's
exit criteria: no secret input/rendering, exact-user binding, provider-state behavior,
reconnect/verify/revoke/delete, capacity/lease display, and the local (non-browser,
non-network) host-side account-affinity declare step that makes "Bind" possible at all.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="ui19-ai-accounts-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"J" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "ui19-test:v1"

import store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from adapters import agent_host, agent_host_enrollment  # noqa: E402
from scripts.frontend_test_source import read_frontend_source  # noqa: E402
from switchboard.api.routers.provider_credentials import create_router  # noqa: E402
from switchboard.application.commands import provider_credentials as commands  # noqa: E402
from switchboard.domain.provider_capacity import account_fingerprint  # noqa: E402
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    CredentialVaultError,
    default_provider_credential_repository as repository,
)
from switchboard.mcp.tools import provider_credentials as mcp_tools  # noqa: E402


PROJECT = "switchboard"
USER_A = "user-ui19-a"
USER_B = "user-ui19-b"
HOST_UNDECLARED = "host/ui19-fresh-install"
HOST_DECLARED = "host/ui19-declared"
PROVIDER = "openai-codex"
ACCOUNT_ID = "steve@example.test"
FINGERPRINT = account_fingerprint(PROVIDER, ACCOUNT_ID)
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def denied(callable_, code):
    try:
        callable_()
    except CredentialVaultError as exc:
        return exc.code == code
    return False


store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="ui19-test")
store.set_project_access(
    PROJECT, store.DEFAULT_ORG_ID, purpose="UI-19 fixture", created_by="ui19-test")
store.init_db(PROJECT)
for user_id in (USER_A, USER_B):
    store.ensure_user(user_id, f"{user_id}@example.test", user_id, created_by="ui19-test")
    store.add_org_member(store.DEFAULT_ORG_ID, user_id, role="member", created_by="ui19-test")


def _placement(*, owner_user_ids, providers, account_affinity_ids):
    return {
        "schema": "switchboard.agent_host_placement.v1",
        "host_class": "persistent",
        "wakeable": True,
        "drain_state": "accepting",
        "supports_credential_leases": True,
        "projects": [PROJECT],
        "tenant_ids": [store.DEFAULT_ORG_ID],
        "owner_user_ids": owner_user_ids,
        "providers": providers,
        "account_affinity_ids": account_affinity_ids,
    }


# A host that completed ADAPTER-18 install/login but has not yet run declare-account —
# this is the realistic default state: owner+provider are known, no affinity yet.
store.register_host({
    "host_id": HOST_UNDECLARED,
    "hostname": "ui19-fresh.test",
    "runtimes": [{"runtime": "codex", "capabilities": ["code"]}],
    "capacity": {
        "active_sessions": 0, "max_sessions": 1,
        "placement": _placement(
            owner_user_ids=[USER_A], providers=[PROVIDER], account_affinity_ids=[]),
    },
    "heartbeat_ttl_s": 3600,
}, principal_id="host-principal-ui19-fresh", actor="ui19-test", project=PROJECT)

# A host that has additionally run declare-account for ACCOUNT_ID.
store.register_host({
    "host_id": HOST_DECLARED,
    "hostname": "ui19-declared.test",
    "runtimes": [{"runtime": "codex", "capabilities": ["code"]}],
    "capacity": {
        "active_sessions": 0, "max_sessions": 1,
        "placement": _placement(
            owner_user_ids=[USER_A], providers=[PROVIDER],
            account_affinity_ids=[FINGERPRINT]),
    },
    "heartbeat_ttl_s": 3600,
}, principal_id="host-principal-ui19-declared", actor="ui19-test", project=PROJECT)


# ---------------------------------------------------------------------------
# 1. "Bind this connection" fails closed before declare-account ever ran.
# ---------------------------------------------------------------------------
bind_payload = {
    "project": PROJECT, "provider": PROVIDER, "provider_account_id": ACCOUNT_ID,
    "project_allowlist": [PROJECT], "host_id": HOST_UNDECLARED,
}
ok(denied(lambda: commands.bind_host_native_mapping(
    bind_payload, actor=USER_A, principal_user_id=USER_A, raise_errors=True),
    "provider_native_proof_invalid"),
   "bind-host fails closed against a host that has not declared this account affinity")

# ---------------------------------------------------------------------------
# 2. Once declared, bind-host succeeds and the proof is derived server-side —
#    the caller never supplies (or needs) an enrollment_proof at all.
# ---------------------------------------------------------------------------
ok("enrollment_proof" not in bind_payload,
   "the browser-facing bind payload carries no proof field — it is derived server-side")
bound = commands.bind_host_native_mapping(
    {**bind_payload, "host_id": HOST_DECLARED},
    actor=USER_A, principal_user_id=USER_A, raise_errors=True)
reference = bound.get("execution_connection_id")
ok(bool(reference) and bound.get("materialization_mode") == "host_native"
   and bound.get("credential_present") is False
   and bound.get("connection_kind") == "personal_subscription"
   and ACCOUNT_ID not in json.dumps(bound.get("ownership_proof") or {}),
   "bind-host succeeds against a host that already declared the account, yields redacted proof")

# ---------------------------------------------------------------------------
# 2b. bind-host must not hardcode Codex's auth_type for every provider — Cursor
#     (a live Connect target per CO-15's supported_host_bound state) needs its
#     own default, never sent by the Connect button either.
# ---------------------------------------------------------------------------
CURSOR_ACCOUNT_ID = "steve-cursor@example.test"
CURSOR_FINGERPRINT = account_fingerprint("cursor", CURSOR_ACCOUNT_ID)
store.register_host({
    "host_id": "host/ui19-cursor-declared",
    "hostname": "ui19-cursor.test",
    "runtimes": [{"runtime": "cursor", "capabilities": ["code"]}],
    "capacity": {
        "active_sessions": 0, "max_sessions": 1,
        "placement": _placement(
            owner_user_ids=[USER_A], providers=["cursor"],
            account_affinity_ids=[CURSOR_FINGERPRINT]),
    },
    "heartbeat_ttl_s": 3600,
}, principal_id="host-principal-ui19-cursor", actor="ui19-test", project=PROJECT)
cursor_bound = commands.bind_host_native_mapping(
    {"project": PROJECT, "provider": "cursor", "provider_account_id": CURSOR_ACCOUNT_ID,
     "project_allowlist": [PROJECT], "host_id": "host/ui19-cursor-declared"},
    actor=USER_A, principal_user_id=USER_A, raise_errors=True)
ok(cursor_bound.get("auth_type") == "browser_login"
   and cursor_bound.get("materialization_mode") == "host_native",
   "bind-host defaults Cursor's auth_type to a real alias of its supported_host_bound "
   "record, not Codex's chatgpt_personal (which is not one of Cursor's aliases at all)")

# ---------------------------------------------------------------------------
# 3. Verify re-attests without rotating, and stamps last_verified_at.
# ---------------------------------------------------------------------------
before = repository.get_metadata(reference, project=PROJECT, principal_user_id=USER_A)
ok(before.get("last_verified_at") is None,
   "a freshly bound connection has no verification timestamp yet")
# The real Settings "Verify" button POSTs an empty body — this must succeed on
# its own by deriving the proof server-side, exactly like this call does.
verified = commands.verify_mapping(
    {"project": PROJECT, "credential_reference": reference},
    actor=USER_A, principal_user_id=USER_A, principal_kind="user", raise_errors=True)
ok(verified.get("last_verified_at") is not None
   and verified.get("last_verified_by") == USER_A
   and verified.get("credential_version") == before.get("credential_version"),
   "verify stamps last_verified_at/by without bumping credential_version (no rotation)")

# ---------------------------------------------------------------------------
# 3b. Reconnect (rotate on a host_native connection) also has no proof to send
#     from the browser — an empty body must derive one the same way verify does,
#     and must NOT wipe the connection's existing host_allowlist in the process.
# ---------------------------------------------------------------------------
reconnected = commands.rotate_mapping(
    {"project": PROJECT, "credential_reference": reference},
    actor=USER_A, principal_user_id=USER_A, principal_kind="user", raise_errors=True)
ok(reconnected.get("host_allowlist") == [HOST_DECLARED],
   "reconnect via empty body derives its own proof and preserves host_allowlist")

# ---------------------------------------------------------------------------
# 4. Verify is scoped to provider-native connections only.
# ---------------------------------------------------------------------------
api_key_connection = commands.enroll_mapping(
    {
        "project": PROJECT, "user_id": USER_A, "provider": "anthropic-claude",
        "provider_account_id": "api-key-account", "auth_type": "api_key",
        "project_allowlist": [PROJECT], "connection_kind": "direct_api",
        "billing_account_id": "billing-ui19-a",
        "budget_policy": {"budget_id": "budget-ui19-a", "currency": "USD", "ceiling": 25},
        "credential": "sk-not-a-real-key",
    },
    actor=USER_A, principal_user_id=USER_A, principal_kind="user",
    trusted_provider_native=True, raise_errors=True)
ok(denied(lambda: commands.verify_mapping(
    {"project": PROJECT, "credential_reference": api_key_connection["execution_connection_id"]},
    actor=USER_A, principal_user_id=USER_A, principal_kind="user", raise_errors=True),
    "provider_native_verification_required"),
   "verify refuses a vault_envelope (API-key) connection — native re-attestation only")

# ---------------------------------------------------------------------------
# 5. active_lease_count is present (opt-in) and accurate on read paths — opt-in
#    so the hot ProviderRuntimeAuth.run() launch path (which never reads this
#    field) doesn't pay for the extra query on every task/session launch.
# ---------------------------------------------------------------------------
metadata_default = repository.get_metadata(reference, project=PROJECT, principal_user_id=USER_A)
ok("active_lease_count" not in metadata_default,
   "get_metadata omits active_lease_count by default (opt-in, not the hot-path default)")
metadata = repository.get_metadata(
    reference, project=PROJECT, principal_user_id=USER_A, include_lease_count=True)
listing = repository.list_metadata(project=PROJECT, principal_user_id=USER_A)
ok(metadata.get("active_lease_count") == 0
   and all("active_lease_count" in item for item in listing),
   "get_metadata(include_lease_count=True) and list_metadata both report active_lease_count")

# ---------------------------------------------------------------------------
# 6. Cross-user denial over REST: user B cannot bind against A's declared host,
#    verify A's connection, or see it in a list — mirrors ENFORCE-8's own pattern.
# ---------------------------------------------------------------------------
principal_state = {"user_id": USER_B}


def principal_record() -> dict:
    return {
        "id": principal_state["user_id"], "kind": "user",
        "effective_scopes": ["read:credentials", "write:credentials"],
    }


api = FastAPI()
api.include_router(create_router(
    resolve_project=lambda value: value,
    resolve_principal=lambda *_args, **_kwargs: principal_record(),
))
client = TestClient(api)

rest_bind = client.post(
    f"/api/projects/{PROJECT}/provider-connections/bind-host",
    json={"provider": PROVIDER, "provider_account_id": ACCOUNT_ID,
          "project_allowlist": [PROJECT], "host_id": HOST_DECLARED})
rest_verify = client.post(
    f"/api/projects/{PROJECT}/provider-connections/{reference}/verify")
rest_list = client.get(f"/api/projects/{PROJECT}/provider-connections")
ok(rest_bind.status_code in (403, 409)
   and rest_verify.status_code == 404
   and rest_list.status_code == 200 and rest_list.json() == {"connections": []},
   "REST denies user B binding via A's host, verifying A's connection, or listing it")

mcp_tools.register_provider_credential_tools(
    type("FakeMCP", (), {"tool": lambda self: (lambda fn: fn)})(),
    mcp_tools.ProviderCredentialToolServices(
        dumps=lambda value: json.dumps(value, sort_keys=True),
        require_read=lambda *_args, **_kwargs: principal_record(),
        require_write=lambda *_args, **_kwargs: principal_record(),
        principal_actor=lambda value: str(value.get("id") or ""),
    ),
)
mcp_verify = json.loads(mcp_tools.verify_provider_connection(reference, None, project=PROJECT))
mcp_bind = json.loads(mcp_tools.bind_host_native_provider_connection(
    json.dumps({"provider": PROVIDER, "provider_account_id": ACCOUNT_ID,
                "project_allowlist": [PROJECT], "host_id": HOST_DECLARED}),
    None, project=PROJECT))
ok(mcp_verify.get("error") == "credential_not_available"
   and mcp_bind.get("error") in ("provider_native_proof_invalid", "provider_native_host_not_attested"),
   "MCP denies user B verifying A's connection or binding via A's declared host")

# ---------------------------------------------------------------------------
# 7. Bind-host never accepts a secret-shaped field, even if a caller tries.
# ---------------------------------------------------------------------------
ok(denied(lambda: commands.bind_host_native_mapping(
    {**bind_payload, "host_id": HOST_DECLARED, "credential": "sneaked-in-secret"},
    actor=USER_A, principal_user_id=USER_A, raise_errors=True),
    "provider_native_enrollment_required"),
   "bind-host rejects any request payload containing a secret-shaped field")

# ---------------------------------------------------------------------------
# 8. Host-side declare-account: a pure local filesystem operation that computes
#    the identical fingerprint formula CO-6 uses, and agent_host.py picks it up.
# ---------------------------------------------------------------------------
host_tmp = Path(tempfile.mkdtemp(prefix="ui19-declare-", dir=TMP))
identity_path = host_tmp / "identity.json"
config_path = host_tmp / "config.json"
identity_path.write_text(json.dumps({
    "host_id": HOST_DECLARED, "host_token": "not-a-real-token", "project": PROJECT,
}), encoding="utf-8")
config_path.write_text(json.dumps({
    "project": PROJECT, "owner_user_id": USER_A, "platform": "", "service_path": "",
}), encoding="utf-8")
declared = agent_host_enrollment.declare_account_affinity(
    identity_path=identity_path, config_path=config_path, project=PROJECT,
    provider=PROVIDER, account_id=ACCOUNT_ID)
ok(declared.get("account_fingerprint") == FINGERPRINT and declared.get("declared") is True
   and declared.get("service_restarted") is False,
   "declare-account computes the exact CO-6 fingerprint formula (no service to restart in the fixture)")
declarations_file = config_path.parent / "account_affinities.json"
ok(declarations_file.is_file()
   and oct(declarations_file.stat().st_mode)[-3:] == "600"
   and FINGERPRINT in json.loads(declarations_file.read_text()).get("account_affinity_ids", []),
   "declare-account persists a 0600 local file containing only the redacted fingerprint")
ok(ACCOUNT_ID not in declarations_file.read_text(),
   "the declarations file never contains the raw, non-fingerprinted account identifier")

prior_environ = dict(os.environ)
try:
    os.environ["PM_AGENT_HOST_CONFIG_PATH"] = str(config_path)
    os.environ.pop("PM_HOST_ACCOUNT_AFFINITIES", None)
    os.environ["PM_HOST_OWNER_USER_ID"] = USER_A
    os.environ.pop("PM_HOST_OWNER_USERS", None)
    os.environ["PM_HOST_PROVIDERS"] = PROVIDER
    picked_up = agent_host._declared_account_affinities()
    ok(picked_up == [FINGERPRINT],
       "agent_host.py reads the locally declared affinity back off disk")
finally:
    os.environ.clear()
    os.environ.update(prior_environ)

# ---------------------------------------------------------------------------
# 9. Frontend source: no secret input, action wiring present, CO-15 states,
#    and ENFORCE-8's own pre-existing needle stays intact.
# ---------------------------------------------------------------------------
settings_source = (ROOT / "static" / "js" / "settings.js").read_text()
frontend_source = read_frontend_source(ROOT)
ok('name="credential"' not in settings_source and 'name="password"' not in settings_source
   and 'name="api_key"' not in settings_source and 'name="token"' not in settings_source
   and "provider-native and owner-bound" in settings_source,
   "settings.js still contains no secret-shaped input and keeps ENFORCE-8's own needle")
ok("_settingsAiAccountsSection" in frontend_source
   and "ai-accounts" in frontend_source,
   "the Personal AI accounts section stays registered in the Settings shell")
for action in ("ai-accounts-connect", "ai-accounts-verify",
               "ai-accounts-reconnect", "ai-accounts-revoke", "ai-accounts-delete"):
    ok(action in settings_source, f"settings.js wires the {action} action")
for state in ("supported", "supported_host_bound", "vendor_confirmation_required", "unavailable"):
    ok(state in settings_source, f"settings.js renders the CO-15 {state} state")

# ---------------------------------------------------------------------------
# 10. Frontend logic (Node): host-candidate detection against the REAL REST
#     response shape, and lifecycle_state filtering — the exact two bugs an
#     independent review caught (h.placement doesn't exist; only h.capacity.
#     placement does, and a revoked/deleted row was blocking Connect forever).
# ---------------------------------------------------------------------------
node_check = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
global.window = {};
eval(src);
const m = window.SwitchboardSettings.methods;

// This is the actual shape GET ixp/v1/agent_hosts returns (_host_row()):
// placement lives ONLY under capacity.placement, never top-level.
const realHost = {
  host_id: 'host/dev-laptop', stale: false,
  capacity: { placement: {
    owner_user_ids: ['user-a'], providers: ['openai-codex'],
    account_affinity_ids: ['acct-deadbeef'],
  } },
};
const placement = m._settingsAiAccountHostPlacement(realHost);
if (placement.owner_user_ids?.[0] !== 'user-a') {
  console.error('placement_not_read_from_capacity', JSON.stringify(placement));
  process.exit(2);
}
const candidates = m._settingsAiAccountCandidateHosts.call(m, [realHost], 'user-a', 'openai-codex');
if (candidates.length !== 1) {
  console.error('candidate_host_not_detected', candidates.length);
  process.exit(3);
}
const noMatch = m._settingsAiAccountCandidateHosts.call(m, [realHost], 'user-a', 'cursor');
if (noMatch.length !== 0) {
  console.error('candidate_host_matched_wrong_provider', noMatch.length);
  process.exit(4);
}

// A revoked connection must not block the Connect UI from ever showing again.
const revoked = { provider: 'openai-codex', connection_kind: 'personal_subscription', lifecycle_state: 'revoked' };
const active = { provider: 'openai-codex', connection_kind: 'personal_subscription', lifecycle_state: 'active' };
const isPersonal = (k) => k === 'personal_subscription';
const onlyRevoked = m._settingsAiAccountLiveConnection([revoked], 'openai-codex', isPersonal);
if (onlyRevoked) {
  console.error('revoked_connection_treated_as_live', JSON.stringify(onlyRevoked));
  process.exit(5);
}
const withActive = m._settingsAiAccountLiveConnection([revoked, active], 'openai-codex', isPersonal);
if (!withActive || withActive.lifecycle_state !== 'active') {
  console.error('active_connection_not_found_among_revoked', JSON.stringify(withActive));
  process.exit(6);
}
console.log('ui19_frontend_ok');
"""
settings_path = ROOT / "static" / "js" / "settings.js"
run = subprocess.run(
    ["node", "-e", node_check, str(settings_path)],
    capture_output=True, text=True, cwd=str(ROOT),
)
ok(run.returncode == 0 and "ui19_frontend_ok" in (run.stdout or ""),
   f"Node: host-candidate detection reads capacity.placement, lifecycle_state filters revoked/deleted (rc={run.returncode})")
if run.returncode != 0:
    print((run.stderr or run.stdout or "")[:800])

print(f"\nUI-19 personal AI accounts settings: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
