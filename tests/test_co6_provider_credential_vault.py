#!/usr/bin/env python3
"""CO-6: encrypted BYOA vault, exact launch binding, and redaction proof."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from path_setup import ROOT


TMP = tempfile.mkdtemp(prefix="co6-provider-vault-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"C" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "co6-test:v1"

from fastapi.testclient import TestClient  # noqa: E402

import mcp_server  # noqa: E402
import store  # noqa: E402
from app import app, _write_required_scopes  # noqa: E402
from switchboard.api.routers.provider_credentials import _access as rest_access  # noqa: E402
from switchboard.application.commands.provider_credentials import (  # noqa: E402
    _require_user_authority,
    acquire_lease_mapping,
    release_lease_mapping,
    start_with_provider_credential,
)
from switchboard.mcp.tools.provider_credentials import _access as mcp_access  # noqa: E402
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    CredentialVaultError,
    default_provider_credential_repository as repository,
)
from switchboard.domain.provider_credentials import CredentialPrincipal  # noqa: E402
from switchboard.domain.provider_capacity import account_fingerprint  # noqa: E402


PROJECT = "switchboard"
OTHER_TENANT_PROJECT = "other-tenant"
USER_ID = "user-co6-owner"
TASK_ID = ""
HOST_ID = "co6-host"
RUNNER_ID = "co6-runner"
WORK_SESSION_ID = "co6-work-session"
AGENT_ID = "codex/CO-600"
PRINCIPAL_ID = "dev-open"
WAKE_ID = "wake-co6-binding"
PRINCIPAL = CredentialPrincipal.from_mapping({
    "principal_id": PRINCIPAL_ID,
    "principal_kind": "system",
    "scopes": ["use:credentials", "admin"],
})
passed = failed = 0
surface_payloads: list[str] = []


def exact_lease_binding(provider: str, account: str) -> dict[str, str]:
    return {
        "claim_id": CLAIM_ID,
        "wake_id": WAKE_ID,
        "account_affinity_id": account_fingerprint(provider, account),
    }


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def body_text(value) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    surface_payloads.append(text)
    return text


def _event_has_secret_fields(row: dict) -> bool:
    """References and versions are provenance; only credential material is secret."""
    secret_fields = {
        "auth_capsule", "credential", "credential_nonce", "encrypted_credential",
        "plaintext", "secret", "token",
    }
    return bool(secret_fields.intersection(row))


client = TestClient(app)

try:
    ok(ROOT.is_dir(), "shared test path shim resolves the repository root")
    store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="co6-test")
    store.set_project_access(
        PROJECT, store.DEFAULT_ORG_ID, purpose="CO-6 fixture", created_by="co6-test")
    store.set_project_access(
        "helm", store.DEFAULT_ORG_ID, purpose="Cross-project fixture", created_by="co6-test")
    store.ensure_user(USER_ID, "co6@example.test", "CO-6 owner", created_by="co6-test")
    store.add_org_member(store.DEFAULT_ORG_ID, USER_ID, role="member", created_by="co6-test")
    store.create_project(
        "Other tenant", project_id=OTHER_TENANT_PROJECT, actor="co6-test",
        org_id="org-co6-other", purpose="Cross-tenant negative fixture")
    store.init_db(OTHER_TENANT_PROJECT)

    task = store.create_task({
        "workstream_id": "CO",
        "workstream_name": "CO",
        "title": "CO-6 binding fixture",
        "status": "Not Started",
    }, actor="co6-test", project=PROJECT)
    TASK_ID = str((task or {}).get("task_id") or "")
    ok(bool(TASK_ID), "created exact task binding fixture")

    work_session = store.create_work_session({
        "work_session_id": WORK_SESSION_ID,
        "task_id": TASK_ID,
        "agent_id": AGENT_ID,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{TASK_ID}-binding-fixture",
        "upstream": "origin/master",
        "base_sha": "a" * 40,
        "head_sha": "a" * 40,
        "storage_mode": "worktree",
        "worktree_path": str(Path(TMP) / "worktree"),
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="co6-test", principal_id=PRINCIPAL_ID, project=PROJECT)
    ok(work_session.get("created") is True, "created active Work Session binding fixture")

    claimed = store.claim_task(
        TASK_ID, AGENT_ID, principal_id=PRINCIPAL_ID, actor="co6-test",
        ttl_seconds=3600, idem_key="co6-binding-fixture",
        work_session_id=WORK_SESSION_ID, session_policy_profile="code_strict",
        require_work_session=True, project=PROJECT,
    )
    CLAIM_ID = str(claimed.get("claim_id") or "")
    ok(claimed.get("claimed") is True and CLAIM_ID,
       "created exact claim/agent/principal Work Session binding fixture")

    host = store.register_host({
        "host_id": HOST_ID,
        "hostname": "co6-host.test",
        "runtimes": [{"runtime": "codex", "capabilities": ["code"]}],
        "capacity": {"active_sessions": 0, "max_sessions": 2},
        "heartbeat_ttl_s": 3600,
    }, principal_id=PRINCIPAL_ID, actor="co6-test", project=PROJECT)
    runner = store.upsert_runner_session({
        "runner_session_id": RUNNER_ID,
        "host_id": HOST_ID,
        "agent_id": AGENT_ID,
        "runtime": "codex",
        "task_id": TASK_ID,
        "claim_id": CLAIM_ID,
        "status": "ready",
        "heartbeat_ttl_s": 3600,
        "metadata": {
            "wake_id": WAKE_ID,
            "work_session_id": WORK_SESSION_ID,
            "credential_reference": "pending",
            "provider_account_id": "acct-openai-personal",
        },
    }, principal_id=PRINCIPAL_ID, actor="co6-test", project=PROJECT)
    ok(host.get("host_id") == HOST_ID and runner.get("runner_session_id") == RUNNER_ID,
       "registered live host and runner-session binding fixtures")

    missing_key_secret = "co6-missing-key-" + uuid.uuid4().hex
    configured_key = os.environ.pop("PM_PROVIDER_VAULT_KEY")
    try:
        repository.enroll(
            project=PROJECT, user_id=USER_ID, provider="codex",
            provider_account_id="acct-missing-key", auth_type="oauth_capsule",
            credential=missing_key_secret, project_allowlist=[PROJECT], actor="co6-test")
        missing_key_error = ""
    except CredentialVaultError as exc:
        missing_key_error = exc.code
    os.environ["PM_PROVIDER_VAULT_KEY"] = configured_key
    ok(missing_key_error == "vault_key_unavailable",
       "missing master key fails closed without persisting or returning the capsule")

    github_secret = "co6-github-" + uuid.uuid4().hex
    try:
        repository.enroll(
            project=PROJECT, user_id=USER_ID, provider="codex",
            provider_account_id="acct-github-must-stay-separate", auth_type="github_app",
            credential=github_secret, project_allowlist=[PROJECT], actor="co6-test")
        github_error = ""
    except CredentialVaultError as exc:
        github_error = exc.code
    ok(github_error == "github_authorization_separate",
       "GitHub repository authorization cannot be enrolled as provider authentication")

    secret_v1 = "co6-v1-" + uuid.uuid4().hex
    enrolled = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="codex",
        provider_account_id="acct-openai-personal", auth_type="oauth_capsule",
        credential=secret_v1, project_allowlist=[PROJECT], actor="co6-test",
        concurrency_policy={"mode": "exclusive", "max_parallel": 1},
        expires_at=time.time() + 3600)
    reference = enrolled.get("credential_reference")
    ok(bool(reference)
       and enrolled.get("provider") == "openai-codex"
       and enrolled.get("credential_present") is True,
       "trusted vault enrollment returns only normalized non-secret metadata")
    runner = store.upsert_runner_session({
        "runner_session_id": RUNNER_ID,
        "host_id": HOST_ID,
        "agent_id": AGENT_ID,
        "runtime": "codex",
        "task_id": TASK_ID,
        "claim_id": CLAIM_ID,
        "status": "ready",
        "heartbeat_ttl_s": 3600,
        "metadata": {
            "wake_id": WAKE_ID,
            "account_affinity_id": account_fingerprint(
                "openai-codex", "acct-openai-personal"),
            "work_session_id": WORK_SESSION_ID,
            "credential_reference": reference,
            "provider_account_id": "acct-openai-personal",
        },
    }, principal_id=PRINCIPAL_ID, actor="co6-test", project=PROJECT)
    ok(secret_v1 not in body_text(enrolled)
       and "encrypted_credential" not in enrolled and "credential_nonce" not in enrolled,
       "vault enrollment never returns raw or encrypted credential material")

    metadata = client.get(
        f"/api/projects/{PROJECT}/provider-connections/{reference}").json()
    ok(metadata.get("credential_reference") == reference
       and metadata.get("events", [{}])[0].get("event_type") == "enrolled"
       and secret_v1 not in body_text(metadata),
       "metadata/audit read preserves provenance without exposing the auth capsule")

    mcp_secret = "co6-mcp-" + uuid.uuid4().hex
    mcp_enrolled_raw = mcp_server.enroll_provider_connection(
        json.dumps({
            "user_id": USER_ID,
            "provider": "cursor",
            "provider_account_id": "acct-cursor-personal",
            "auth_type": "personal_api_key",
            "credential": mcp_secret,
            "project_allowlist": [PROJECT],
        }),
        None,
        project=PROJECT,
    )
    mcp_enrolled = json.loads(mcp_enrolled_raw)
    surface_payloads.append(mcp_enrolled_raw)
    ok(mcp_enrolled.get("error") == "provider_native_enrollment_required"
       and mcp_secret not in mcp_enrolled_raw,
       "MCP refuses raw provider credential enrollment without echoing the secret")
    cursor_connection = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="cursor",
        provider_account_id="acct-cursor-personal", auth_type="personal_api_key",
        credential=mcp_secret, project_allowlist=[PROJECT], actor="co6-test")
    listed = client.get(f"/api/projects/{PROJECT}/provider-connections").json()
    ok(len(listed.get("connections") or []) == 2,
       "tenant administrator can list all visible user connections without a user filter")

    binding = {
        "project": PROJECT,
        "credential_reference": reference,
        "user_id": USER_ID,
        "provider": "openai-codex",
        "provider_account_id": "acct-openai-personal",
        "task_id": TASK_ID,
        "host_id": HOST_ID,
        "runner_session_id": RUNNER_ID,
        "work_session_id": WORK_SESSION_ID,
        **exact_lease_binding("openai-codex", "acct-openai-personal"),
        "ttl_seconds": 900,
    }
    store.upsert_runner_session({
        "runner_session_id": RUNNER_ID, "host_id": HOST_ID, "agent_id": AGENT_ID,
        "runtime": "codex", "task_id": TASK_ID, "claim_id": CLAIM_ID,
        "status": "completed", "heartbeat_ttl_s": 3600,
        "metadata": {"wake_id": WAKE_ID, "work_session_id": WORK_SESSION_ID,
                     "account_affinity_id": binding["account_affinity_id"],
                     "credential_reference": reference,
                     "provider_account_id": "acct-openai-personal"},
    }, principal_id=PRINCIPAL_ID, actor="co6-test", project=PROJECT)
    terminal_response = client.post(
        f"/api/projects/{PROJECT}/provider-connections/{reference}/leases",
        json={key: value for key, value in binding.items()
              if key not in {"project", "credential_reference"}},
    )
    store.upsert_runner_session({
        "runner_session_id": RUNNER_ID, "host_id": HOST_ID, "agent_id": AGENT_ID,
        "runtime": "codex", "task_id": TASK_ID, "claim_id": CLAIM_ID,
        "status": "ready", "heartbeat_ttl_s": 3600,
        "metadata": {"wake_id": WAKE_ID, "work_session_id": WORK_SESSION_ID,
                     "account_affinity_id": binding["account_affinity_id"],
                     "credential_reference": reference,
                     "provider_account_id": "acct-openai-personal"},
    }, principal_id=PRINCIPAL_ID, actor="co6-test", project=PROJECT)
    ok(terminal_response.status_code == 409
       and terminal_response.json().get("detail", {}).get("error")
       == "credential_runner_binding_invalid",
       "terminal runner session fails before a credential lease is issued")

    lease_response = client.post(
        f"/api/projects/{PROJECT}/provider-connections/{reference}/leases",
        json={key: value for key, value in binding.items()
              if key not in {"project", "credential_reference"}},
    )
    lease = lease_response.json()
    lease_id = lease.get("lease_id")
    ok(lease_response.status_code == 200 and lease_id
       and all(lease.get(key) == binding[key] for key in (
           "task_id", "host_id", "runner_session_id", "work_session_id")),
       "dispatch lease persists the full user/account/project/task/host/runner/work binding")
    ok(secret_v1 not in body_text(lease), "lease receipt contains no credential material")

    starts: list[str] = []

    def starter(credential: str):
        starts.append(credential)
        return {"started": True, "pid": 4242, "credential": credential,
                "debug": {"secret": credential}}

    launched = start_with_provider_credential(
        binding, lease_id=lease_id, actor="co6-test-runner", start_process=starter,
        principal=PRINCIPAL)
    ok(launched == {"allowed": True, "started": True, "pid": 4242}
       and starts == [secret_v1] and secret_v1 not in body_text(launched),
       "trusted bridge decrypts only after exact binding and allowlists its launch receipt")

    before_denials = len(starts)
    wrong_provider = start_with_provider_credential(
        {**binding, "provider": "cursor"}, lease_id=lease_id,
        actor="co6-test-runner", start_process=starter, principal=PRINCIPAL,
        validate_runtime=False)
    wrong_project = start_with_provider_credential(
        {**binding, "project": "helm"}, lease_id=lease_id,
        actor="co6-test-runner", start_process=starter, principal=PRINCIPAL,
        validate_runtime=False)
    cross_tenant = start_with_provider_credential(
        {**binding, "project": OTHER_TENANT_PROJECT}, lease_id=lease_id,
        actor="co6-test-runner", start_process=starter, principal=PRINCIPAL,
        validate_runtime=False)
    unbound = start_with_provider_credential(
        binding, lease_id="provider-lease-unbound", actor="co6-test-runner",
        start_process=starter, principal=PRINCIPAL, validate_runtime=False)
    ok(not any(item.get("allowed") for item in (
        wrong_provider, wrong_project, cross_tenant, unbound))
       and len(starts) == before_denials,
       "wrong-provider, cross-project, cross-tenant, and unbound launches fail before start")

    try:
        repository.acquire_lease(
            project=PROJECT, credential_reference=reference, user_id=USER_ID,
            provider="openai-codex", provider_account_id="acct-openai-personal",
            task_id=TASK_ID, host_id=HOST_ID, runner_session_id="another-runner",
            work_session_id=WORK_SESSION_ID, ttl_seconds=900, actor="co6-test",
            principal=PRINCIPAL,
            **exact_lease_binding("openai-codex", "acct-openai-personal"))

        concurrency_denied = False
    except CredentialVaultError as exc:
        concurrency_denied = exc.code == "credential_concurrency_exhausted"
    ok(concurrency_denied, "exclusive personal account cannot be pooled across concurrent lanes")

    released_rest = client.post(
        f"/api/projects/{PROJECT}/provider-credential-leases/{lease_id}/release",
        json={"reason": "REST structured-principal round trip"},
    )
    ok(released_rest.status_code == 200
       and released_rest.json().get("state") == "released",
       "REST preserves the structured system principal through acquire and release")

    mcp_ref = cursor_connection["credential_reference"]
    mcp_binding = {
        **binding,
        "credential_reference": mcp_ref,
        "provider": "cursor",
        "provider_account_id": "acct-cursor-personal",
        "account_affinity_id": account_fingerprint(
            "cursor", "acct-cursor-personal"),
    }
    store.upsert_runner_session({
        "runner_session_id": RUNNER_ID, "host_id": HOST_ID, "agent_id": AGENT_ID,
        "runtime": "cursor", "task_id": TASK_ID, "claim_id": CLAIM_ID,
        "status": "ready", "heartbeat_ttl_s": 3600,
        "metadata": {"wake_id": WAKE_ID, "work_session_id": WORK_SESSION_ID,
                     "account_affinity_id": account_fingerprint(
                         "cursor", "acct-cursor-personal"),
                     "credential_reference": mcp_ref,
                     "provider_account_id": "acct-cursor-personal"},
    }, principal_id=PRINCIPAL_ID, actor="co6-test", project=PROJECT)
    mcp_lease_raw = mcp_server.acquire_provider_credential_lease(
        json.dumps({key: value for key, value in mcp_binding.items()
                    if key != "project"}),
        None, project=PROJECT,
    )
    mcp_lease = json.loads(mcp_lease_raw)
    mcp_release_raw = mcp_server.release_provider_credential_lease(
        mcp_lease.get("lease_id") or "", "MCP structured-principal round trip",
        None, project=PROJECT,
    )
    store.upsert_runner_session({
        "runner_session_id": RUNNER_ID, "host_id": HOST_ID, "agent_id": AGENT_ID,
        "runtime": "codex", "task_id": TASK_ID, "claim_id": CLAIM_ID,
        "status": "ready", "heartbeat_ttl_s": 3600,
        "metadata": {"wake_id": WAKE_ID, "work_session_id": WORK_SESSION_ID,
                     "account_affinity_id": binding["account_affinity_id"],
                     "credential_reference": reference,
                     "provider_account_id": "acct-openai-personal"},
    }, principal_id=PRINCIPAL_ID, actor="co6-test", project=PROJECT)
    surface_payloads.extend([mcp_lease_raw, mcp_release_raw])
    ok(mcp_lease.get("state") == "issued"
       and json.loads(mcp_release_raw).get("state") == "released",
       "MCP preserves the structured system principal through acquire and release")

    agent_secret = "co6-agent-release-" + uuid.uuid4().hex
    agent_connection = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="codex",
        provider_account_id="acct-agent-release", auth_type="oauth_capsule",
        credential=agent_secret, project_allowlist=[PROJECT], actor="co6-test",
        expires_at=time.time() + 3600)
    agent_principal = rest_access({
        "id": "agent-release", "kind": "agent", "scopes": ["use:credentials"],
    })
    agent_lease = acquire_lease_mapping(
        {**binding, "credential_reference": agent_connection["credential_reference"],
         "provider_account_id": "acct-agent-release"},
        actor="agent-release", principal=agent_principal,
        validate_runtime=False, raise_errors=True)
    cross_service = release_lease_mapping(
        {"project": PROJECT, "lease_id": agent_lease["lease_id"],
         "reason": "cross-service negative"},
        actor="other-agent", principal=rest_access({
            "id": "other-agent", "kind": "agent", "scopes": ["use:credentials"],
        }))
    agent_released = release_lease_mapping(
        {"project": PROJECT, "lease_id": agent_lease["lease_id"],
         "reason": "exact agent release"},
        actor="agent-release", principal=agent_principal, raise_errors=True)
    ok(cross_service.get("error") == "credential_lease_release_denied"
       and agent_released.get("state") == "released",
       "REST actor adapter permits exact agent release and denies cross-service release")

    host_secret = "co6-host-release-" + uuid.uuid4().hex
    host_connection = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="codex",
        provider_account_id="acct-host-release", auth_type="oauth_capsule",
        credential=host_secret, project_allowlist=[PROJECT], actor="co6-test",
        expires_at=time.time() + 3600)
    host_principal = mcp_access({
        "id": HOST_ID, "kind": "host", "scopes": ["use:credentials"],
    })
    host_lease = acquire_lease_mapping(
        {**binding, "credential_reference": host_connection["credential_reference"],
         "provider_account_id": "acct-host-release"},
        actor=HOST_ID, principal=host_principal,
        validate_runtime=False, raise_errors=True)
    host_released = release_lease_mapping(
        {"project": PROJECT, "lease_id": host_lease["lease_id"],
         "reason": "exact host release"},
        actor=HOST_ID, principal=host_principal, raise_errors=True)
    ok(host_released.get("state") == "released",
       "MCP actor adapter preserves the exact host principal through acquire and release")

    secret_v2 = "co6-v2-" + uuid.uuid4().hex
    rotated_raw = mcp_server.rotate_provider_connection(
        reference,
        json.dumps({"credential": secret_v2, "expires_at": time.time() + 7200}),
        None,
        project=PROJECT,
    )
    rotated = json.loads(rotated_raw)
    surface_payloads.append(rotated_raw)
    ok(rotated.get("error") == "provider_native_enrollment_required",
       "MCP refuses raw provider credential rotation")
    rotated = repository.rotate(
        reference, project=PROJECT, credential=secret_v2, actor="co6-test",
        expires_at=time.time() + 7200, principal_user_id=USER_ID)
    after_rotation = start_with_provider_credential(
        binding, lease_id=lease_id, actor="co6-test-runner", start_process=starter,
        principal=PRINCIPAL)
    ok(rotated.get("credential_version") == 2
       and secret_v2 not in rotated_raw and after_rotation.get("allowed") is False
       and len(starts) == before_denials,
       "rotation encrypts the new capsule and fences every lease on the prior version")

    lease_v2 = repository.acquire_lease(
        project=PROJECT, credential_reference=reference, user_id=USER_ID,
        provider="openai-codex", provider_account_id="acct-openai-personal",
        task_id=TASK_ID, host_id=HOST_ID, runner_session_id=RUNNER_ID,
        work_session_id=WORK_SESSION_ID, ttl_seconds=900, actor="co6-test",
        principal=PRINCIPAL,
        **exact_lease_binding("openai-codex", "acct-openai-personal"))
    started_v2 = start_with_provider_credential(
        binding, lease_id=lease_v2["lease_id"], actor="co6-test-runner",
        start_process=starter, principal=PRINCIPAL)
    ok(started_v2.get("allowed") is True and starts[-1] == secret_v2,
       "fresh lease materializes only the rotated credential version")

    store.upsert_runner_session({
        "runner_session_id": RUNNER_ID, "host_id": HOST_ID, "agent_id": AGENT_ID,
        "runtime": "codex", "task_id": TASK_ID, "claim_id": CLAIM_ID,
        "status": "ready", "heartbeat_ttl_s": 3600,
        "control": {"managed_process": True, "runner_kill": True},
        "metadata": {"wake_id": WAKE_ID, "work_session_id": WORK_SESSION_ID,
                     "account_affinity_id": binding["account_affinity_id"],
                     "credential_reference": reference,
                     "provider_account_id": "acct-openai-personal"},
    }, principal_id=PRINCIPAL_ID, actor="co6-test", project=PROJECT)
    revoked_response = client.post(
        f"/api/projects/{PROJECT}/provider-connections/{reference}/revoke",
        json={"reason": "operator revocation proof"},
    )
    revoked = revoked_response.json()
    starts_before_revoked = len(starts)
    revoked_launch = start_with_provider_credential(
        binding, lease_id=lease_v2["lease_id"], actor="co6-test-runner",
        start_process=starter, principal=PRINCIPAL)
    ok(revoked_response.status_code == 200
       and revoked.get("lifecycle_state") == "revoked"
       and revoked.get("credential_present") is False
       and revoked.get("runner_cleanup", {}).get("binding_count") == 1
       and revoked.get("runner_cleanup", {}).get("requested_count") == 1
       and revoked_launch.get("allowed") is False
       and len(starts) == starts_before_revoked,
       "revocation erases ciphertext, fences leases, requests runner kill, and blocks launch")

    rotate_revoked = client.post(
        f"/api/projects/{PROJECT}/provider-connections/{reference}/rotate",
        json={"credential": "must-not-store-" + uuid.uuid4().hex},
    )
    try:
        repository.rotate(
            reference, project=PROJECT, credential="must-not-store-internal",
            actor="co6-test", principal_user_id=USER_ID)
        revoked_rotate_error = ""
    except CredentialVaultError as exc:
        revoked_rotate_error = exc.code
    ok(rotate_revoked.status_code == 400
       and rotate_revoked.json().get("detail", {}).get("error")
       == "provider_native_enrollment_required"
       and revoked_rotate_error == "credential_not_rotatable",
       "revoked credential cannot be silently reactivated by rotation")

    deleted_response = client.request(
        "DELETE",
        f"/api/projects/{PROJECT}/provider-connections/{reference}",
        json={"reason": "customer deletion proof"},
    )
    deleted = deleted_response.json()
    ok(deleted_response.status_code == 200
       and deleted.get("lifecycle_state") == "deleted"
       and deleted.get("credential_present") is False,
       "delete flow leaves an auditable tombstone after cryptographic erasure")

    expiring_secret = "co6-expired-" + uuid.uuid4().hex
    expiring = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="claude",
        provider_account_id="acct-claude-personal", auth_type="oauth_capsule",
        credential=expiring_secret, project_allowlist=[PROJECT], actor="co6-test",
        expires_at=time.time() + 3600)
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.execute(
            "UPDATE provider_connections SET expires_at=?, lifecycle_state='active' "
            "WHERE credential_reference=?",
            (time.time() - 1, expiring["credential_reference"]),
        )
    try:
        repository.acquire_lease(
            project=PROJECT, credential_reference=expiring["credential_reference"],
            user_id=USER_ID, provider="anthropic-claude",
            provider_account_id="acct-claude-personal", task_id=TASK_ID,
            host_id=HOST_ID, runner_session_id=RUNNER_ID,
            work_session_id=WORK_SESSION_ID, ttl_seconds=900, actor="co6-test",
            principal=PRINCIPAL,
            **exact_lease_binding("anthropic-claude", "acct-claude-personal"))
        expired_denied = False
    except CredentialVaultError as exc:
        expired_denied = exc.code == "credential_not_usable"
    expired_metadata = repository.get_metadata(
        expiring["credential_reference"], project=PROJECT, admin=True)
    ok(expired_denied and expired_metadata.get("lifecycle_state") == "expired",
       "expired credential state fails closed before a launch lease is issued")

    corrupt_secret = "co6-corrupt-" + uuid.uuid4().hex
    corrupt = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="codex",
        provider_account_id="acct-corrupt-proof", auth_type="oauth_capsule",
        credential=corrupt_secret, project_allowlist=[PROJECT], actor="co6-test",
        expires_at=time.time() + 3600)
    corrupt_binding = {
        **binding,
        "credential_reference": corrupt["credential_reference"],
        "provider_account_id": "acct-corrupt-proof",
    }
    corrupt_lease = repository.acquire_lease(
        project=PROJECT, credential_reference=corrupt["credential_reference"],
        user_id=USER_ID, provider="openai-codex",
        provider_account_id="acct-corrupt-proof", task_id=TASK_ID,
        host_id=HOST_ID, runner_session_id=RUNNER_ID,
        work_session_id=WORK_SESSION_ID, ttl_seconds=900, actor="co6-test",
        principal=PRINCIPAL,
        **exact_lease_binding("openai-codex", "acct-corrupt-proof"))
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.execute(
            "UPDATE provider_connections SET encrypted_credential=? "
            "WHERE credential_reference=?",
            (b"invalid-authenticated-ciphertext", corrupt["credential_reference"]),
        )
    before_corrupt = len(starts)
    corrupt_launch = start_with_provider_credential(
        corrupt_binding, lease_id=corrupt_lease["lease_id"],
        actor="co6-test-runner", start_process=starter, principal=PRINCIPAL,
        validate_runtime=False)
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        corrupt_lease_state = c.execute(
            "SELECT state FROM provider_credential_leases WHERE lease_id=?",
            (corrupt_lease["lease_id"],),
        ).fetchone()[0]
    ok(corrupt_launch.get("allowed") is False
       and corrupt_lease_state == "fenced" and len(starts) == before_corrupt,
       "ciphertext/key authentication failure fences the lease before process start")

    replay_secret = "co6-replay-" + uuid.uuid4().hex
    replay = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="codex",
        provider_account_id="acct-replay-proof", auth_type="oauth_capsule",
        credential=replay_secret, project_allowlist=[PROJECT], actor="co6-test",
        expires_at=time.time() + 3600)
    replay_binding = {**binding, "credential_reference": replay["credential_reference"],
                      "provider_account_id": "acct-replay-proof"}
    replay_lease = repository.acquire_lease(
        project=PROJECT, credential_reference=replay["credential_reference"],
        user_id=USER_ID, provider="openai-codex",
        provider_account_id="acct-replay-proof", task_id=TASK_ID,
        host_id=HOST_ID, runner_session_id=RUNNER_ID,
        work_session_id=WORK_SESSION_ID, ttl_seconds=900, actor="co6-test",
        principal=PRINCIPAL,
        **exact_lease_binding("openai-codex", "acct-replay-proof"))

    def materialize_replay():
        try:
            value = repository.materialize_for_runtime(
                replay_lease["lease_id"], actor="co6-replay",
                principal=PRINCIPAL, **{key: replay_binding[key] for key in (
                    "project", "user_id", "provider", "provider_account_id", "task_id",
                    "host_id", "runner_session_id", "work_session_id")})
            return "materialized" if value == replay_secret else "wrong_secret"
        except CredentialVaultError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_results = list(executor.map(lambda _: materialize_replay(), range(2)))
    repository.fence_materialized_lease(
        replay_lease["lease_id"], actor="co6-test", reason="replay_test_cleanup",
        principal=PRINCIPAL)
    ok(sorted(replay_results) == ["credential_lease_already_consumed", "materialized"],
       "atomic issued-to-materializing transition permits exactly one concurrent decrypt")

    failure_secret = "co6-start-failure-" + uuid.uuid4().hex
    failure = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="codex",
        provider_account_id="acct-start-failure", auth_type="oauth_capsule",
        credential=failure_secret, project_allowlist=[PROJECT], actor="co6-test",
        expires_at=time.time() + 3600)
    failure_binding = {**binding, "credential_reference": failure["credential_reference"],
                       "provider_account_id": "acct-start-failure"}
    failure_lease = repository.acquire_lease(
        project=PROJECT, credential_reference=failure["credential_reference"],
        user_id=USER_ID, provider="openai-codex",
        provider_account_id="acct-start-failure", task_id=TASK_ID,
        host_id=HOST_ID, runner_session_id=RUNNER_ID,
        work_session_id=WORK_SESSION_ID, ttl_seconds=900, actor="co6-test",
        principal=PRINCIPAL,
        **exact_lease_binding("openai-codex", "acct-start-failure"))
    purges: list[bool] = []

    def failed_starter(_credential: str):
        raise RuntimeError("provider start failed")

    failed_launch = start_with_provider_credential(
        failure_binding, lease_id=failure_lease["lease_id"], actor="co6-test",
        start_process=failed_starter, principal=PRINCIPAL,
        purge_runtime=lambda: purges.append(True), validate_runtime=False)
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        failed_state = c.execute(
            "SELECT state FROM provider_credential_leases WHERE lease_id=?",
            (failure_lease["lease_id"],),
        ).fetchone()[0]
    ok(failed_launch.get("allowed") is False and failed_state == "fenced"
       and purges == [True],
       "process-start failure purges runtime residue and permanently fences the lease")

    activation_secret = "co6-activation-binding-" + uuid.uuid4().hex
    activation = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="codex",
        provider_account_id="acct-activation-binding", auth_type="oauth_capsule",
        credential=activation_secret, project_allowlist=[PROJECT], actor="co6-test",
        expires_at=time.time() + 3600)
    activation_binding = {
        **binding,
        "credential_reference": activation["credential_reference"],
        "provider_account_id": "acct-activation-binding",
    }
    activation_lease = repository.acquire_lease(
        project=PROJECT, credential_reference=activation["credential_reference"],
        user_id=USER_ID, provider="openai-codex",
        provider_account_id="acct-activation-binding", task_id=TASK_ID,
        host_id=HOST_ID, runner_session_id=RUNNER_ID,
        work_session_id=WORK_SESSION_ID, ttl_seconds=900, actor="co6-test",
        principal=PRINCIPAL,
        **exact_lease_binding("openai-codex", "acct-activation-binding"))
    repository.materialize_for_runtime(
        activation_lease["lease_id"], actor="co6-test", principal=PRINCIPAL,
        **{key: activation_binding[key] for key in (
            "project", "user_id", "provider", "provider_account_id", "task_id",
            "host_id", "runner_session_id", "work_session_id")})
    try:
        repository.activate_materialized_lease(
            activation_lease["lease_id"], actor="co6-test", principal=PRINCIPAL,
            expected_binding={**activation_binding, "host_id": "host-other"})
        wrong_activation_denied = False
    except CredentialVaultError as exc:
        wrong_activation_denied = exc.code == "credential_binding_mismatch"
    activated = repository.activate_materialized_lease(
        activation_lease["lease_id"], actor="co6-test", principal=PRINCIPAL,
        expected_binding=activation_binding)
    ok(wrong_activation_denied and activated.get("state") == "active",
       "materialized lease activation requires the exact lease runtime binding")

    expiry_secret = "co6-materializing-expiry-" + uuid.uuid4().hex
    expiry = repository.enroll(
        project=PROJECT, user_id=USER_ID, provider="codex",
        provider_account_id="acct-materializing-expiry", auth_type="oauth_capsule",
        credential=expiry_secret, project_allowlist=[PROJECT], actor="co6-test",
        expires_at=time.time() + 3600)
    expiry_binding = {**binding, "credential_reference": expiry["credential_reference"],
                      "provider_account_id": "acct-materializing-expiry"}
    expiry_lease = repository.acquire_lease(
        project=PROJECT, credential_reference=expiry["credential_reference"],
        user_id=USER_ID, provider="openai-codex",
        provider_account_id="acct-materializing-expiry", task_id=TASK_ID,
        host_id=HOST_ID, runner_session_id=RUNNER_ID,
        work_session_id=WORK_SESSION_ID, ttl_seconds=900, actor="co6-test",
        principal=PRINCIPAL,
        **exact_lease_binding("openai-codex", "acct-materializing-expiry"))
    repository.materialize_for_runtime(
        expiry_lease["lease_id"], actor="co6-test", principal=PRINCIPAL,
        **{key: expiry_binding[key] for key in (
            "project", "user_id", "provider", "provider_account_id", "task_id",
            "host_id", "runner_session_id", "work_session_id")})
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.execute("UPDATE provider_credential_leases SET expires_at=? WHERE lease_id=?",
                  (time.time() - 1, expiry_lease["lease_id"]))
    try:
        repository.activate_materialized_lease(
            expiry_lease["lease_id"], actor="co6-test", principal=PRINCIPAL)
        expiry_denied = False
    except CredentialVaultError:
        expiry_denied = True
    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        expiry_state = c.execute(
            "SELECT state FROM provider_credential_leases WHERE lease_id=?",
            (expiry_lease["lease_id"],),
        ).fetchone()[0]
    ok(expiry_denied and expiry_state == "expired",
       "a lease expiring while materializing cannot become active")

    mcp_revoked_raw = mcp_server.revoke_provider_connection(
        mcp_ref, "MCP revocation proof", None, project=PROJECT)
    mcp_deleted_raw = mcp_server.delete_provider_connection(
        mcp_ref, "MCP deletion proof", None, project=PROJECT)
    surface_payloads.extend([mcp_revoked_raw, mcp_deleted_raw])
    ok(json.loads(mcp_revoked_raw).get("lifecycle_state") == "revoked"
       and json.loads(mcp_deleted_raw).get("lifecycle_state") == "deleted",
       "MCP lifecycle adapters share the same revoke/delete state machine")

    with sqlite3.connect(os.environ["PM_PROJECT_REGISTRY_DB_PATH"]) as c:
        c.row_factory = sqlite3.Row
        stored = c.execute(
            "SELECT encrypted_credential, credential_nonce, key_id FROM provider_connections "
            "WHERE credential_reference=?",
            (expiring["credential_reference"],),
        ).fetchone()
        event_rows = c.execute("SELECT * FROM provider_credential_events").fetchall()
        lease_rows = c.execute("SELECT * FROM provider_credential_leases").fetchall()
    ok(stored["encrypted_credential"]
       and expiring_secret.encode() not in bytes(stored["encrypted_credential"])
       and stored["credential_nonce"] and stored["key_id"] == "co6-test:v1",
       "registry persists AES-GCM ciphertext/nonce/key-version, never plaintext")
    ok(all(not _event_has_secret_fields(dict(row)) for row in event_rows)
       and all(secret not in body_text([dict(row) for row in event_rows + lease_rows])
               for secret in (secret_v1, secret_v2, mcp_secret, expiring_secret)),
       "audit and lease rows contain only non-secret affinity/provenance")

    with sqlite3.connect(os.environ["PM_SWITCHBOARD_DB_PATH"]) as c:
        history = "\n".join(str(row[0] or "") for row in c.execute(
            "SELECT payload FROM activity").fetchall())
    all_secrets = (
        missing_key_secret, github_secret, secret_v1, secret_v2, mcp_secret,
        expiring_secret, corrupt_secret, replay_secret, failure_secret, expiry_secret,
        agent_secret, host_secret,
    )
    ok(all(secret not in history for secret in all_secrets),
       "task history contains no credential or auth capsule")
    ok(all(secret not in payload for payload in surface_payloads for secret in all_secrets),
       "all captured REST/MCP responses and launch receipts are secret-free")

    artifact_bytes = b"".join(
        path.read_bytes() for path in Path(TMP).rglob("*")
        if path.is_file()
    )
    ok(all(secret.encode() not in artifact_bytes for secret in all_secrets),
       "SQLite/WAL/cache artifacts contain no plaintext credential residue")
    ok(_write_required_scopes(
        f"/api/projects/{PROJECT}/provider-connections") == ("write:credentials",)
       and _write_required_scopes(
           f"/api/projects/{PROJECT}/provider-connections/{reference}/leases")
       == ("use:credentials",),
       "global HTTP gate enforces dedicated manage/use credential scopes")
    human_access = {
        "id": "different-user", "kind": "human", "scopes": ["use:credentials"],
    }
    service_access = {
        "id": "runner-service", "kind": "agent", "scopes": ["use:credentials"],
    }
    denied_human = False
    try:
        _require_user_authority(USER_ID, CredentialPrincipal.from_mapping(rest_access(human_access)))
    except CredentialVaultError:
        denied_human = True
    _require_user_authority(
        USER_ID, CredentialPrincipal.from_mapping(mcp_access(service_access)))
    ok(denied_human
       and rest_access(human_access)["principal_kind"] == "human"
       and mcp_access(service_access)["principal_kind"] == "agent",
       "use scope does not let a human impersonate another provider identity")

finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nCO-6 provider credential vault: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
