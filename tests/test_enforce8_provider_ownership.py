#!/usr/bin/env python3
"""ENFORCE-8: two-user ownership, execution binding, and no-fallback proof."""
from __future__ import annotations

import atexit
import base64
import json
import os
from pathlib import Path
import shutil
import tempfile

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="enforce8-provider-ownership-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"E" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "enforce8-test:v1"

import store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from switchboard.api.routers.provider_credentials import create_router  # noqa: E402
from switchboard.application.commands import provider_credentials as commands  # noqa: E402
from switchboard.domain.provider_capacity import account_fingerprint  # noqa: E402
from switchboard.domain.provider_credentials import (  # noqa: E402
    CredentialPrincipal,
    normalize_execution_connection_policy,
)
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    CredentialVaultError,
    default_provider_credential_repository as repository,
)
from switchboard.mcp.tools import provider_credentials as mcp_tools  # noqa: E402


PROJECT = "switchboard"
USER_A = "user-enforce8-a"
USER_B = "user-enforce8-b"
HOST_A = "host/enforce8-a"
ACCOUNT_A = "provider-account-a"
FINGERPRINT_A = account_fingerprint("codex", ACCOUNT_A)
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


store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="enforce8-test")
store.set_project_access(
    PROJECT, store.DEFAULT_ORG_ID, purpose="ENFORCE-8 fixture", created_by="enforce8-test")
store.init_db(PROJECT)
for user_id in (USER_A, USER_B):
    store.ensure_user(
        user_id, f"{user_id}@example.test", user_id, created_by="enforce8-test")
    store.add_org_member(
        store.DEFAULT_ORG_ID, user_id, role="member", created_by="enforce8-test")

host = store.register_host({
    "host_id": HOST_A,
    "hostname": "enforce8-a.test",
    "runtimes": [{"runtime": "codex", "capabilities": ["code"]}],
    "capacity": {
        "active_sessions": 0,
        "max_sessions": 1,
        "placement": {
            "schema": "switchboard.agent_host_placement.v1",
            "host_class": "persistent",
            "wakeable": True,
            "drain_state": "accepting",
            "supports_credential_leases": True,
            "projects": [PROJECT],
            "tenant_ids": [store.DEFAULT_ORG_ID],
            "owner_user_ids": [USER_A],
            "providers": ["openai-codex"],
            "account_affinity_ids": [FINGERPRINT_A],
        },
    },
    "heartbeat_ttl_s": 3600,
}, principal_id="host-principal-enforce8", actor="enforce8-test", project=PROJECT)
ok(host.get("host_id") == HOST_A and not host.get("stale"),
   "provider-native proof starts from a live registered host inventory")

native_payload = {
    "project": PROJECT,
    "user_id": USER_A,
    "provider": "codex",
    "provider_account_id": ACCOUNT_A,
    "auth_type": "oauth_capsule",
    "project_allowlist": [PROJECT],
    "host_allowlist": [HOST_A],
    "enrollment_proof": {
        "proof_id": "proof-enforce8-a",
        "host_id": HOST_A,
        "account_fingerprint": FINGERPRINT_A,
        "verified": True,
    },
}
connection = commands.enroll_mapping(
    native_payload, actor=USER_A, principal_user_id=USER_A,
    principal_kind="user", repository=repository, raise_errors=True)
reference = connection.get("execution_connection_id")
proof = connection.get("ownership_proof") or {}
ok(bool(reference) and connection.get("materialization_mode") == "host_native"
   and connection.get("credential_present") is False
   and proof.get("account_fingerprint") == FINGERPRINT_A
   and ACCOUNT_A not in json.dumps(proof),
   "secret-free provider-native enrollment yields redacted owner proof")

raw_secret = "must-never-cross-public-transport"
raw_result = commands.enroll_mapping(
    {**native_payload, "credential": raw_secret}, actor=USER_A,
    principal_user_id=USER_A, principal_kind="user", repository=repository)
ok(raw_result.get("error") == "provider_native_enrollment_required"
   and raw_secret not in json.dumps(raw_result),
   "REST/MCP shared command rejects raw credentials without echoing them")
secret_aliases_denied = all(
    commands.enroll_mapping(
        {**native_payload, "nested": {field: raw_secret}}, actor=USER_A,
        principal_user_id=USER_A, principal_kind="user", repository=repository,
    ).get("error") == "provider_native_enrollment_required"
    for field in (
        "api_key", "access_token", "authorization", "client_secret",
        "cookie_jar", "oauth_token", "session_token",
    )
)
ok(secret_aliases_denied,
   "public enrollment rejects nested provider-secret aliases before validation")
rotated_native = commands.rotate_mapping(
    {
        "project": PROJECT, "credential_reference": reference,
        "host_allowlist": [HOST_A],
        "enrollment_proof": {
            **native_payload["enrollment_proof"],
            "proof_id": "proof-enforce8-a-refresh",
        },
    },
    actor=USER_A, principal_user_id=USER_A, principal_kind="user",
    repository=repository, raise_errors=True,
)
ok(rotated_native.get("credential_version") == 2
   and rotated_native.get("credential_present") is False,
   "owner refreshes provider-native proof without submitting credential material")

ok(denied(lambda: commands.enroll_mapping(
    {**native_payload, "user_id": USER_B}, actor=USER_B,
    principal_user_id=USER_B, principal_kind="user", repository=repository,
    raise_errors=True), "provider_native_proof_invalid"),
   "a second user cannot reuse another owner's host-verified account proof")
ok(denied(lambda: commands.enroll_mapping(
    native_payload, actor=USER_B, principal_user_id=USER_B,
    principal_kind="user", repository=repository, raise_errors=True),
    "provider_owner_action_required"),
   "user B cannot enroll a connection owned by user A")

visible_b = repository.list_metadata(
    project=PROJECT, principal_user_id=USER_B, admin=False)
ok(visible_b == []
   and denied(lambda: repository.get_metadata(
       reference, project=PROJECT, principal_user_id=USER_B, admin=False),
       "credential_not_available"),
   "user B cannot list, get, or infer user A's connection")
ok(denied(lambda: repository.revoke(
    reference, project=PROJECT, actor=USER_B, reason="cross-user",
    principal_user_id=USER_B, admin=False), "credential_not_available")
   and denied(lambda: repository.delete(
       reference, project=PROJECT, actor=USER_B, reason="cross-user",
       principal_user_id=USER_B, admin=False), "credential_not_available"),
   "user B cannot revoke or delete user A's connection")

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
rest_list = client.get(f"/api/projects/{PROJECT}/provider-connections")
rest_get = client.get(
    f"/api/projects/{PROJECT}/provider-connections/{reference}")
rest_enroll = client.post(
    f"/api/projects/{PROJECT}/provider-connections", json=native_payload)
rest_revoke = client.post(
    f"/api/projects/{PROJECT}/provider-connections/{reference}/revoke",
    json={"reason": "cross-user"},
)
rest_delete = client.request(
    "DELETE", f"/api/projects/{PROJECT}/provider-connections/{reference}",
    json={"reason": "cross-user"},
)
ok(rest_list.status_code == 200 and rest_list.json() == {"connections": []}
   and rest_get.status_code == 404 and rest_enroll.status_code == 403
   and rest_revoke.status_code == 404 and rest_delete.status_code == 404,
   "REST prevents user B from listing, inferring, enrolling, revoking, or deleting user A's connection")


class FakeMCP:
    def tool(self):
        return lambda fn: fn


mcp_tools.register_provider_credential_tools(
    FakeMCP(),
    mcp_tools.ProviderCredentialToolServices(
        dumps=lambda value: json.dumps(value, sort_keys=True),
        require_read=lambda *_args, **_kwargs: principal_record(),
        require_write=lambda *_args, **_kwargs: principal_record(),
        principal_actor=lambda value: str(value.get("id") or ""),
    ),
)
mcp_list = json.loads(mcp_tools.list_provider_connections(None, project=PROJECT))
mcp_get = json.loads(mcp_tools.get_provider_connection(reference, None, project=PROJECT))
mcp_enroll = json.loads(mcp_tools.enroll_provider_connection(
    json.dumps(native_payload), None, project=PROJECT))
mcp_revoke = json.loads(mcp_tools.revoke_provider_connection(
    reference, "cross-user", None, project=PROJECT))
mcp_delete = json.loads(mcp_tools.delete_provider_connection(
    reference, "cross-user", None, project=PROJECT))
ok(mcp_list == {"connections": []}
   and mcp_get.get("error") == "credential_not_available"
   and mcp_enroll.get("error") == "provider_owner_action_required"
   and mcp_revoke.get("error") == "credential_not_available"
   and mcp_delete.get("error") == "credential_not_available",
   "MCP prevents user B from listing, inferring, enrolling, revoking, or deleting user A's connection")

host_principal = CredentialPrincipal.from_mapping({
    "principal_id": "host-principal-enforce8", "principal_kind": "host",
    "scopes": ["use:credentials"],
})
ok(denied(lambda: repository.acquire_lease(
    project=PROJECT, credential_reference=reference, user_id=USER_A,
    provider="codex", provider_account_id=ACCOUNT_A, task_id="ENFORCE-8",
    host_id=HOST_A, runner_session_id="runner-enforce8-a",
    work_session_id="worksession-enforce8-a", ttl_seconds=900,
    actor=HOST_A, principal=host_principal,
    host_classes=["trusted_private_worker", "user_owned_persistent"]),
    "credential_binding_incomplete"),
   "repository refuses leases missing claim, wake, and account affinity")
lease = repository.acquire_lease(
    project=PROJECT, credential_reference=reference, user_id=USER_A,
    provider="codex", provider_account_id=ACCOUNT_A, task_id="ENFORCE-8",
    host_id=HOST_A, runner_session_id="runner-enforce8-a",
    work_session_id="worksession-enforce8-a", claim_id="claim-enforce8-a",
    wake_id="wake-enforce8-a", account_affinity_id=FINGERPRINT_A,
    execution_connection_id=reference, expected_tenant_id=store.DEFAULT_ORG_ID,
    ttl_seconds=900, actor=HOST_A, principal=host_principal,
    host_classes=["trusted_private_worker", "user_owned_persistent"])
ok(all(lease.get(key) for key in (
    "execution_connection_id", "claim_id", "wake_id", "account_affinity_id"))
   and lease.get("connection_kind") == "personal_subscription",
   "lease persists connection/customer/project/task/claim/work/runner/host/wake binding")
native_inputs: list[object] = []
native_launch = commands.start_with_provider_credential(
    {
        "project": PROJECT, "credential_reference": reference,
        "user_id": USER_A, "provider": "codex",
        "provider_account_id": ACCOUNT_A, "task_id": "ENFORCE-8",
        "host_id": HOST_A, "runner_session_id": "runner-enforce8-a",
        "work_session_id": "worksession-enforce8-a", "claim_id": "claim-enforce8-a",
        "wake_id": "wake-enforce8-a", "account_affinity_id": FINGERPRINT_A,
    },
    lease_id=lease["lease_id"], actor=HOST_A, principal=host_principal,
    start_process=lambda credential: (
        native_inputs.append(credential)
        or {"started": credential is None, "runner_session_id": "runner-enforce8-a"}
    ),
    repository=repository, validate_runtime=False,
)
ok(native_launch.get("allowed") is True and native_inputs == [None],
   "runner starts host-native connection without decrypting or receiving a secret")
repository.release_lease(
    lease["lease_id"], project=PROJECT, actor=HOST_A,
    reason="host_native_test_complete", principal=host_principal)
ok(denied(lambda: repository.acquire_lease(
    project=PROJECT, credential_reference=reference, user_id=USER_A,
    provider="codex", provider_account_id=ACCOUNT_A, task_id="ENFORCE-8",
    host_id="host/other", runner_session_id="runner-other",
    work_session_id="worksession-other", claim_id="claim-other", wake_id="wake-other",
    account_affinity_id=FINGERPRINT_A, execution_connection_id=reference,
    ttl_seconds=900, actor="host/other", principal=host_principal),
    "credential_host_affinity_denied"),
   "approved-host affinity fails closed before materialization")
ok(denied(lambda: repository.acquire_lease(
    project=PROJECT, credential_reference=reference, user_id=USER_B,
    provider="codex", provider_account_id=ACCOUNT_A, task_id="ENFORCE-8",
    host_id=HOST_A, runner_session_id="runner-b", work_session_id="work-b",
    claim_id="claim-b", wake_id="wake-b", account_affinity_id=FINGERPRINT_A,
    execution_connection_id=reference, ttl_seconds=900, actor=HOST_A,
    principal=host_principal), "credential_not_available"),
   "lease acquisition cannot substitute another user's account")

personal = normalize_execution_connection_policy({}, connection_kind="personal_subscription")
ok(personal["fallback"] == {"enabled": False},
   "personal subscription exhaustion has no implicit metered fallback")
try:
    normalize_execution_connection_policy({}, connection_kind="direct_api")
    api_policy_denied = False
except Exception as exc:
    api_policy_denied = getattr(exc, "code", "") == "billing_account_required"
api_policy = normalize_execution_connection_policy({
    "budget_policy": {"budget_id": "budget-a", "currency": "USD", "ceiling": 25},
}, connection_kind="direct_api", billing_account_id="billing-a")
ok(api_policy_denied and api_policy["fallback"] == {"enabled": False}
   and api_policy["budget"]["ceiling"] == 25,
   "API connections require exact billing/budget attribution and still default fallback off")

settings_source = (ROOT / "static" / "js" / "settings.js").read_text()
ok('name="credential"' not in settings_source
   and 'name="password"' not in settings_source
   and "provider-native and owner-bound" in settings_source,
   "browser settings surface is proof-only and contains no raw-secret input")

events = repository.get_metadata(
    reference, project=PROJECT, principal_user_id=USER_A,
    admin=False, include_events=True).get("events") or []
serialized = json.dumps({"connection": connection, "lease": lease, "events": events})
ok(raw_secret not in serialized and "encrypted_credential" not in serialized
   and all(event.get("execution_connection_id") == reference for event in events),
   "connection, lease, and audit receipts remain secret-free and attributable")

print(f"\nENFORCE-8 provider ownership: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
