#!/usr/bin/env python3
"""CO-15: authoritative provider auth matrix and deterministic fail-closed gates."""
from __future__ import annotations

import atexit
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="co15-provider-auth-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"P" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "co15-test:v1"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import store  # noqa: E402
from switchboard.api.routers.provider_credentials import create_router  # noqa: E402
from switchboard.application.commands import provider_credentials as commands  # noqa: E402
from switchboard.domain.provider_credentials import (  # noqa: E402
    CredentialPrincipal,
    auth_host_classes_for_host,
    list_provider_auth_capabilities,
    provider_auth_decision,
)
from switchboard.domain.provider_capacity import account_fingerprint  # noqa: E402
from switchboard.integrations.provider_runtime_auth import ProviderRuntimeAuth  # noqa: E402
from switchboard.mcp.tools import provider_credentials as mcp_tools  # noqa: E402
from switchboard.storage.repositories.provider_capacity import ProviderCapacityRepository  # noqa: E402
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    CredentialVaultError,
    default_provider_credential_repository as repository,
)


PROJECT = "switchboard"
USER_ID = "user-co15-owner"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="co15-test")
store.set_project_access(
    PROJECT, store.DEFAULT_ORG_ID, purpose="CO-15 fixture", created_by="co15-test")
store.init_db(PROJECT)
store.ensure_user(USER_ID, "co15@example.test", "CO-15 owner", created_by="co15-test")
store.add_org_member(store.DEFAULT_ORG_ID, USER_ID, role="member", created_by="co15-test")

matrix = list_provider_auth_capabilities(now=time.time())
records = {row["capability_id"]: row for row in matrix["capabilities"]}
ok(matrix["fail_closed"] is True and len(records) == 8,
   "one versioned server matrix contains every supported and denied auth path")
ok(records["codex-chatgpt-capsule-trusted-private"]["state"] == "supported"
   and records["claude-subscription-switchboard"]["state"]
   == "vendor_confirmation_required"
   and records["cursor-browser-login-user-host"]["state"] == "supported_host_bound"
   and records["cursor-personal-portable-worker"]["state"] == "unavailable"
   and records["claude-host-bound-native-cli"]["state"] == "supported_host_bound",
   "Codex, Claude, and Cursor personal modes have the required authoritative states")
ok(matrix["personal_subscription_broker"]["litellm"] is False
   and not any(row["litellm"]["eligible"] for row in records.values()
               if row["auth_mode"] != "api_key")
   and records["codex-api-key-managed-worker"]["litellm"]["eligible"] is True,
   "LiteLLM is API/paygo-only and can never broker personal subscription login")

cursor_host = provider_auth_decision(
    "cursor", "browser_login", host_class="user_owned_persistent")
cursor_unbound = provider_auth_decision("cursor", "browser_login")
ok(cursor_host["allowed"] is True and cursor_host["state"] == "supported_host_bound"
   and cursor_unbound["allowed"] is False
   and cursor_unbound["reason_code"] == "provider_auth_host_binding_required",
   "Cursor browser auth requires the exact persistent user-owned host binding")
# CO-22: Claude host-bound personal login mirrors the Cursor host-bound posture.
claude_host = provider_auth_decision(
    "claude", "oauth_personal", host_class="user_owned_persistent",
    operation="launch")
claude_unbound = provider_auth_decision(
    "claude", "oauth_personal", operation="launch")
claude_portable = provider_auth_decision(
    "claude", "setup_token_oauth", operation="launch")
ok(claude_host["allowed"] is True
   and claude_host["state"] == "supported_host_bound"
   and claude_host["host_class"] == "user_owned_persistent"
   and claude_unbound["allowed"] is False
   and claude_unbound["reason_code"] == "provider_auth_host_binding_required"
   and claude_portable["allowed"] is False,
   "CO-22: Claude host-bound login is allowed only on a persistent host; unbound "
   "use and the portable setup-token path stay denied")
codex_no_host = provider_auth_decision(
    "codex", "oauth_capsule", operation="launch")
codex_ephemeral = provider_auth_decision(
    "codex", "oauth_capsule", host_classes=["managed_or_ephemeral_worker"],
    operation="launch")
codex_trusted = provider_auth_decision(
    "codex", "oauth_capsule", host_classes=["trusted_private_worker"],
    operation="launch")
ok(not codex_no_host["allowed"]
   and codex_no_host["reason_code"] == "provider_auth_host_class_required"
   and not codex_ephemeral["allowed"]
   and codex_ephemeral["reason_code"] == "provider_auth_host_class_mismatch"
   and codex_trusted["allowed"] is True,
   "Codex personal auth requires a trusted-private worker at execution boundaries")
derived = auth_host_classes_for_host({
    "host_id": "host/co15-dedicated",
    "capacity": {
        "placement": {
            "host_class": "persistent",
            "supports_credential_leases": True,
        }
    },
})
ok(derived == ("trusted_private_worker", "user_owned_persistent"),
   "persistent credential-lease hosts classify as trusted-private + user-owned")
stale = provider_auth_decision(
    "codex", "oauth_capsule", now=time.time() + 2 * 365 * 24 * 3600)
unknown = provider_auth_decision("codex", "scraped_browser_cookie")
ok(not stale["allowed"] and stale["reason_code"] == "provider_auth_policy_evidence_stale"
   and not unknown["allowed"] and unknown["reason_code"] == "provider_auth_mode_unknown",
   "stale evidence and unknown auth modes fail closed with stable reasons")


def enrollment(provider: str, auth_type: str, account: str, **extra) -> dict:
    payload = {
        "project": PROJECT,
        "user_id": USER_ID,
        "provider": provider,
        "provider_account_id": account,
        "auth_type": auth_type,
        "credential": f"co15-secret-{account}",
        "project_allowlist": [PROJECT],
    }
    payload.update(extra)
    return commands.enroll_mapping(
        payload, actor="co15-test", principal_user_id=USER_ID,
        trusted_provider_native=True, repository=repository)


codex = enrollment("codex", "oauth_capsule", "codex-personal")
claude_denied = enrollment("claude", "setup_token_oauth", "claude-personal")
cursor_denied = enrollment("cursor", "session_capsule", "cursor-portable")
cursor_api = enrollment("cursor", "api_key", "cursor-api")
ok(bool(codex.get("credential_reference")) and bool(cursor_api.get("credential_reference")),
   "Codex personal and explicit Cursor API-key enrollment remain available")
ok(claude_denied.get("error_code") == "provider_auth_vendor_confirmation_required"
   and cursor_denied.get("error_code") == "cursor_personal_auth_portability_unavailable",
   "unsupported personal modes are denied before enrollment persists credentials")

codex_bounded = enrollment(
    "codex", "oauth_capsule", "codex-personal-bounded",
    concurrency_policy={"mode": "bounded", "max_parallel": 4},
)
bounded_meta = repository.get_metadata(
    codex_bounded["credential_reference"], project=PROJECT,
    principal_user_id=USER_ID, admin=False,
)
ok(bounded_meta.get("concurrency_policy") == {"mode": "exclusive", "max_parallel": 1},
   "personal subscription enrollment forces exclusive max_parallel=1")

store.register_host({
    "host_id": "host-co15-trusted",
    "hostname": "co15-trusted",
    "runtimes": [{"runtime": "codex", "lanes": ["CO"]}],
    "capacity": {
        "placement": {
            "host_class": "persistent",
            "supports_credential_leases": True,
            "auth_host_classes": [
                "trusted_private_worker", "user_owned_persistent",
            ],
        },
        "active_sessions": 0,
    },
    "heartbeat_ttl_s": 120,
}, principal_id="co15-system", project=PROJECT)
store.register_host({
    "host_id": "host/i-co15-ephemeral",
    "hostname": "co15-ephemeral",
    "runtimes": [{"runtime": "codex", "lanes": ["CO"]}],
    "capacity": {
        "placement": {
            "host_class": "ephemeral",
            "bound_wake_id": "wake-co15",
            "supports_credential_leases": True,
        },
        "active_sessions": 0,
    },
    "heartbeat_ttl_s": 120,
}, principal_id="co15-system", project=PROJECT)

# Seed a legacy Claude subscription row directly to prove that historical data
# cannot bypass the new scheduler, lease, or runtime-launch gates.
legacy = repository.enroll(
    project=PROJECT, user_id=USER_ID, provider="claude",
    provider_account_id="legacy-claude", auth_type="setup_token_oauth",
    credential="legacy-claude-secret", project_allowlist=[PROJECT],
    actor="co15-test", expires_at=time.time() + 3600,
    concurrency_policy={"mode": "exclusive", "max_parallel": 1},
)
binding = {
    "project": PROJECT,
    "credential_reference": legacy["credential_reference"],
    "user_id": USER_ID,
    "provider": "anthropic-claude",
    "provider_account_id": "legacy-claude",
    "task_id": "CO-15",
    "claim_id": "claim-co15",
    "wake_id": "wake-co15-legacy",
    "account_affinity_id": account_fingerprint("claude", "legacy-claude"),
    "host_id": "host-co15-trusted",
    "runner_session_id": "runner-co15",
    "work_session_id": "worksession-co15",
    "ttl_seconds": 900,
}
schedule = ProviderCapacityRepository().admission_decision(
    binding,
    task_policy={"customer_user_id": USER_ID, "requested_provider": "claude"},
)
ok(not schedule["allowed"]
   and schedule["reason_code"] == "provider_auth_vendor_confirmation_required",
   "scheduler rejects a legacy Claude subscription row before dispatch")

principal = CredentialPrincipal.from_mapping({
    "principal_id": "co15-system",
    "principal_kind": "system",
    "scopes": ["use:credentials"],
})
try:
    repository.acquire_lease(
        project=PROJECT, credential_reference=legacy["credential_reference"],
        user_id=USER_ID, provider="claude", provider_account_id="legacy-claude",
        task_id="CO-15", host_id="host-co15-trusted",
        runner_session_id="runner-co15", work_session_id="worksession-co15",
        ttl_seconds=900, actor="co15-test", principal=principal,
        claim_id=binding["claim_id"], wake_id=binding["wake_id"],
        account_affinity_id=binding["account_affinity_id"],
    )
    ok(False, "legacy Claude acquire_lease must fail closed")
except CredentialVaultError as exc:
    ok(exc.code == "provider_auth_vendor_confirmation_required",
       "lease acquire independently rejects the unapproved subscription mode")

codex_binding = {
    "project": PROJECT,
    "credential_reference": codex["credential_reference"],
    "user_id": USER_ID,
    "provider": "openai-codex",
    "provider_account_id": "codex-personal",
    "task_id": "CO-15",
    "claim_id": "claim-co15-codex",
    "wake_id": "wake-co15-codex",
    "account_affinity_id": account_fingerprint("codex", "codex-personal"),
    "host_id": "host-co15-trusted",
    "runner_session_id": "runner-co15-codex",
    "work_session_id": "worksession-co15-codex",
    "ttl_seconds": 900,
}
codex_ok = ProviderCapacityRepository().admission_decision(
    codex_binding,
    task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
)
codex_bad = ProviderCapacityRepository().admission_decision(
    {**codex_binding, "host_id": "host/i-co15-ephemeral",
     "claim_id": "claim-co15-ephemeral",
     "host_placement": {
         "host_class": "ephemeral", "bound_wake_id": "wake-co15",
     }},
    task_policy={"customer_user_id": USER_ID, "requested_provider": "codex"},
)
ok(codex_ok.get("allowed") is True
   and not codex_bad.get("allowed")
   and codex_bad.get("reason_code") == "provider_auth_host_class_mismatch",
   "scheduler allows Codex personal only on trusted-private hosts")

cursor_browser = enrollment(
    "cursor", "browser_login", "cursor-host-bound",
    host_id="host-co15-trusted",
)
ok(bool(cursor_browser.get("credential_reference")),
   "Cursor browser enroll succeeds on a user-owned persistent host")
cursor_schedule = ProviderCapacityRepository().admission_decision(
    {
        "project": PROJECT,
        "credential_reference": cursor_browser["credential_reference"],
        "user_id": USER_ID,
        "provider": "cursor",
        "provider_account_id": "cursor-host-bound",
        "task_id": "CO-15",
        "claim_id": "claim-co15-cursor",
        "host_id": "host-co15-trusted",
        "runner_session_id": "runner-co15-cursor",
        "work_session_id": "worksession-co15-cursor",
    },
    task_policy={"customer_user_id": USER_ID, "requested_provider": "cursor"},
)
ok(cursor_schedule.get("allowed") is True,
   "Cursor host-bound path can progress once authoritative host class is present")

process_calls = []
runtime = ProviderRuntimeAuth(
    repository=repository,
    runtime_parent=Path(TMP) / "runtime",
    command_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    process_factory=lambda *args, **kwargs: process_calls.append(args),
    base_environment={"PATH": os.environ.get("PATH", "")},
)
launch = runtime.run(
    {key: value for key, value in binding.items() if key != "claim_id"},
    lease_id="", principal=principal,
    actor="co15-test", command=["claude", "-p", "test"], validate_runtime=False)
ok(not launch["allowed"]
   and launch["error_code"] == "provider_auth_vendor_confirmation_required"
   and not process_calls,
   "runtime launch denies before materialization/process start")

# REST and MCP expose the same matrix object used above.
api = FastAPI()
api.include_router(create_router(
    resolve_project=lambda value: value,
    resolve_principal=lambda *_args, **_kwargs: {
        "id": USER_ID, "kind": "user", "effective_scopes": ["read:credentials"],
    },
))
rest = TestClient(api).get(f"/api/projects/{PROJECT}/provider-auth-capabilities")
ok(rest.status_code == 200
   and rest.json()["policy_version"] == matrix["policy_version"]
   and len(rest.json()["capabilities"]) == len(matrix["capabilities"]),
   "REST returns the authoritative matrix without a transport-local allowlist")


class FakeMCP:
    def tool(self):
        return lambda fn: fn


registered = mcp_tools.register_provider_credential_tools(
    FakeMCP(),
    mcp_tools.ProviderCredentialToolServices(
        dumps=lambda value: json.dumps(value, sort_keys=True),
        require_read=lambda *_args, **_kwargs: {"id": USER_ID},
        require_write=lambda *_args, **_kwargs: {"id": USER_ID},
        principal_actor=lambda _principal: USER_ID,
    ),
)
mcp_matrix = json.loads(registered["list_provider_auth_capabilities"](None, PROJECT))
ok(mcp_matrix["policy_version"] == matrix["policy_version"]
   and len(mcp_matrix["capabilities"]) == len(matrix["capabilities"]),
   "MCP returns the same authoritative capability records as REST")

settings_source = (ROOT / "static" / "js" / "settings.js").read_text()
proof_source = (ROOT / "static" / "js" / "proof-console.js").read_text()
ok("provider-auth-capabilities" in settings_source
   and "_settingsAiAccountsSection" in settings_source
   and "No authoritative capability records; execution fails closed." in settings_source
   and "provider-auth-capabilities" in proof_source
   and "CO-15 provider auth policy" in proof_source,
   "Settings and CO-14 consume the server record and fail closed when it is missing")

docs = (ROOT / "docs" / "PROVIDER-AUTH-POLICY.md").read_text()
ok("third-party products" in docs and "API/pay-as-you-go" in docs
   and "cross-user leases" in docs,
   "operator documentation preserves vendor, LiteLLM, and per-user isolation boundaries")

print(f"\nCO-15 provider auth policy: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
