#!/usr/bin/env python3
"""ADAPTER-52: attach one live signed Host/provider identity to another project."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT


TMP = Path(tempfile.mkdtemp(prefix="adapter52-project-attach-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import provider_credentials as commands  # noqa: E402
from switchboard.domain.provider_capacity import account_fingerprint  # noqa: E402
from switchboard.domain.provider_credentials import CredentialPrincipal  # noqa: E402
from switchboard.storage.repositories import project_execution_policy  # noqa: E402
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    CredentialVaultError,
    default_provider_credential_repository as repository,
)


SOURCE = "switchboard"
TARGET = "maxwell"
FOREIGN = "adapter52-foreign"
USER = "user/adapter52-owner"
OTHER_USER = "user/adapter52-other"
HOST = "host/adapter52-existing-mac"
HOST_PRINCIPAL = "host-principal-adapter52"
PROVIDER = "openai-codex"
ACCOUNT = "adapter52@example.test"
AFFINITY = account_fingerprint(PROVIDER, ACCOUNT)


def expect_error(call, code: str) -> None:
    try:
        call()
    except CredentialVaultError as exc:
        assert exc.code == code, exc.as_dict()
    else:
        raise AssertionError(f"expected {code}")


def project(project_id: str, org_id: str, repository_name: str) -> None:
    if project_id not in {SOURCE, TARGET}:
        created = store.create_project(
            project_id, project_id=project_id, actor="adapter52-test",
            org_id=org_id, purpose="ADAPTER-52 fixture")
        assert created.get("created") is True, created
    store.init_db(project_id)
    store.set_project_access(
        project_id, org_id, owner_user_id=(USER if org_id == "org-adapter52" else OTHER_USER),
        created_by="adapter52-test")
    store.set_project_repo_topology(
        project=project_id, canonical_repo=repository_name,
        canonical_default_branch="master" if project_id == SOURCE else "main")


try:
    store.ensure_org("org-adapter52", "ADAPTER-52", created_by="adapter52-test")
    store.ensure_org("org-adapter52-foreign", "ADAPTER-52 foreign", created_by="adapter52-test")
    project(SOURCE, "org-adapter52", "6th-Element-Labs/projectplanner")
    project(TARGET, "org-adapter52", "6th-Element-Labs/maxwell")
    project(FOREIGN, "org-adapter52-foreign", "other/foreign")
    for user_id, org_id in ((USER, "org-adapter52"), (OTHER_USER, "org-adapter52-foreign")):
        store.ensure_user(
            user_id, f"{user_id.rsplit('/', 1)[-1]}@example.test", user_id,
            created_by="adapter52-test")
        store.add_org_member(org_id, user_id, role="owner", created_by="adapter52-test")

    now = time.time()
    fingerprint = "sha256:" + "a" * 64
    with _conn(SOURCE) as connection:
        connection.execute(
            "INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (HOST_PRINCIPAL, "agent_host", HOST, SOURCE,
             json.dumps(["read", "write:agent_host"]), "hash", now),
        )
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "execution_policy_json,bootstrap_hash,bootstrap_expires_at,bootstrap_consumed_at,"
            "principal_id,public_key_fingerprint,identity_generation,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hostenroll-adapter52", SOURCE, HOST, HOST, USER,
             json.dumps(["org-adapter52"]), json.dumps([SOURCE]), json.dumps([PROVIDER]),
             "{}", "bootstrap", now + 60, now, HOST_PRINCIPAL, fingerprint,
             7, "active", now, now),
        )

    registered = store.register_host({
        "host_id": HOST,
        "hostname": "adapter52-mac",
        "runtimes": [{
            "runtime": "codex",
            "provider": PROVIDER,
            "lanes": [],
            "capabilities": ["docs", "github", "python", "tests"],
            "policy": {"allow_work": True, "allow_global_claim": False},
            "local_auth": {
                "available": True, "runtime": "codex",
                "provider_credential_exported": False,
            },
        }],
        "limits": {"max_sessions": 8},
        "capacity": {
            "active_sessions": 1,
            "owner": {
                "user_id": USER,
                "tenant_allowlist": ["org-adapter52"],
                "project_allowlist": [SOURCE],
                "provider_allowlist": [PROVIDER],
            },
            "local_auth": {
                "available": True, "runtime": "codex",
                "provider_credential_exported": False,
            },
            "placement": {
                "host_class": "persistent",
                "owner_user_ids": [USER],
                "providers": [PROVIDER],
                "account_affinity_ids": [AFFINITY],
                "supports_scm_materialization": True,
                "scm_providers": ["github_app"],
            },
        },
        "heartbeat_ttl_s": 3600,
    }, principal_id=HOST_PRINCIPAL, actor=HOST, project=SOURCE)
    assert registered.get("host_id") == HOST, registered

    before_enrollment = store.get_agent_host_enrollment(HOST, project=SOURCE)
    grant = store.create_agent_host_project_grant(
        source_project=SOURCE, host_id=HOST, target_project=TARGET,
        canonical_repository="6th-Element-Labs/maxwell", runtime="codex",
        provider=PROVIDER, trust_zone="org_shared", isolation_mode="worktree",
        max_concurrency=2, actor=USER)
    assert grant.get("granted") is True, grant
    after_enrollment = store.get_agent_host_enrollment(HOST, project=SOURCE)
    assert before_enrollment["project_allowlist"] == [SOURCE], before_enrollment
    assert after_enrollment["project_allowlist"] == [TARGET, SOURCE], after_enrollment
    assert after_enrollment["enrollment_id"] == before_enrollment["enrollment_id"]
    assert after_enrollment["identity_generation"] == before_enrollment["identity_generation"]
    assert after_enrollment["public_key_fingerprint"] == fingerprint
    target_identity = store.check_agent_host_identity(
        HOST, HOST_PRINCIPAL, project=TARGET)
    assert target_identity.get("allowed") is True, target_identity
    assert any(item.get("host_id") == HOST for item in store.list_agent_hosts(project=TARGET))

    foreign_grant = store.create_agent_host_project_grant(
        source_project=SOURCE, host_id=HOST, target_project=FOREIGN,
        canonical_repository="other/foreign", runtime="codex", provider=PROVIDER,
        trust_zone="org_shared", isolation_mode="worktree", max_concurrency=1,
        actor=USER)
    assert foreign_grant.get("error_code") == "target_project_access_denied", foreign_grant
    assert store.get_agent_host_enrollment(HOST, project=SOURCE)["project_allowlist"] == [TARGET, SOURCE]

    connection = repository.enroll(
        project=SOURCE, user_id=USER, provider=PROVIDER,
        provider_account_id=ACCOUNT, auth_type="chatgpt_personal", credential="",
        project_allowlist=[SOURCE], actor=USER, host_allowlist=[HOST],
        materialization_mode="host_native")
    reference = str(connection.get("credential_reference") or "")
    verified = repository.verify_host_native(
        reference, project=SOURCE, actor=USER, principal_user_id=USER)
    assert verified.get("execution_ready") is True, verified

    principal = CredentialPrincipal.from_mapping({
        "principal_id": "system/adapter52-runner",
        "principal_kind": "system",
        "scopes": ["use:credentials"],
    })
    binding = {
        "project": SOURCE,
        "user_id": USER,
        "provider": PROVIDER,
        "provider_account_id": ACCOUNT,
        "task_id": "ADAPTER-52-live",
        "host_id": HOST,
        "runner_session_id": "run_adapter52_live",
        "work_session_id": "worksession-adapter52-live",
        "claim_id": "taskclaim-adapter52-live",
        "wake_id": "wake-adapter52-live",
        "account_affinity_id": AFFINITY,
    }
    lease = repository.acquire_lease(
        credential_reference=reference, ttl_seconds=900, actor=HOST,
        principal=principal, host_classes=("trusted_private_worker",),
        execution_connection_id=reference,
        **{key: value for key, value in binding.items()
           if key not in {"claim_id", "wake_id", "account_affinity_id"}},
        claim_id=binding["claim_id"], wake_id=binding["wake_id"],
        account_affinity_id=binding["account_affinity_id"])
    lease_id = str(lease.get("lease_id") or "")
    assert lease.get("state") == "issued" and lease_id, lease
    material = repository.materialize_for_runtime(
        lease_id, actor=HOST, principal=principal,
        **{key: value for key, value in binding.items()
           if key not in {"claim_id", "wake_id", "account_affinity_id"}})
    assert material is None
    active = repository.activate_materialized_lease(
        lease_id, actor=HOST, principal=principal, expected_binding=binding)
    assert active.get("state") == "active", active
    version_before = verified["credential_version"]

    attached = commands.attach_project_mapping(
        {"project": SOURCE, "credential_reference": reference,
         "target_project": TARGET},
        actor=USER, principal_user_id=USER, principal_kind="user",
        raise_errors=True)
    assert attached.get("project_allowlist") == [TARGET, SOURCE], attached
    assert attached.get("credential_version") == version_before, attached
    visible = repository.list_metadata(project=TARGET, principal_user_id=USER)
    assert [item["credential_reference"] for item in visible] == [reference], visible

    events = repository.get_metadata(
        reference, project=SOURCE, principal_user_id=USER, include_events=True)["events"]
    attach_event = next(item for item in events if item["event_type"] == "project_attached")
    assert attach_event["project"] == TARGET and attach_event["actor"] == USER, attach_event
    secret_keys = {"credential", "encrypted_credential", "credential_nonce", "token", "secret"}
    assert not secret_keys.intersection(attach_event), attach_event
    assert not secret_keys.intersection(attach_event.get("details") or {}), attach_event

    expect_error(lambda: commands.attach_project_mapping(
        {"project": SOURCE, "credential_reference": reference,
         "target_project": TARGET}, actor=OTHER_USER, principal_user_id=OTHER_USER,
        principal_kind="user", raise_errors=True), "credential_not_available")
    expect_error(lambda: commands.attach_project_mapping(
        {"project": SOURCE, "credential_reference": reference,
         "target_project": FOREIGN}, actor=USER, principal_user_id=USER,
        principal_kind="user", raise_errors=True), "cross_tenant_allowlist_denied")

    unchanged_lease = repository.release_lease(
        lease_id, project=SOURCE, actor=HOST, reason="adapter52-test-complete",
        principal=principal)
    assert unchanged_lease.get("state") == "released", unchanged_lease
    assert unchanged_lease.get("credential_version") == version_before, unchanged_lease

    class AuthorizedSCM:
        def get(self, _reference):
            return {
                "provider": "github_app", "lifecycle_state": "active",
                "project_allowlist": [TARGET],
                "repository_allowlist": ["6th-element-labs/maxwell"],
                "operation_scopes": ["clone", "fetch", "push", "create_pr"],
            }

    project_execution_policy.default_scm_connection_repository = AuthorizedSCM()
    activated = store.set_project_execution_policy(project=TARGET, actor=USER, updates={
        "runtimes": {"allowed": ["codex"], "default": "codex"},
        "workspace": {"repo_role": "canonical", "isolation": "worktree"},
        "placement": {"host_classes": ["personal"], "trust_zones": ["org_shared"],
                      "burst": {"enabled": False, "max_concurrent_ephemeral": 0}},
        "providers": {"selectors": [{"provider": PROVIDER,
                                      "connection_reference": reference}]},
        "scm": {"provider": "github_app", "connection_reference": "scm-adapter52"},
        "autopilot": {"enabled": False, "profile_id": ""},
        "lifecycle": {"status": "active"},
    })
    assert not activated.get("error"), activated
    assert activated["execution_policy"]["providers"]["selectors"][0][
        "connection_reference"] == reference

    print("ADAPTER-52 host/provider project attachment: PASS")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
