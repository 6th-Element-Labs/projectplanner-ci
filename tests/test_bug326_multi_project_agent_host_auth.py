#!/usr/bin/env python3
"""BUG-326: one enrolled Host bearer works on explicitly granted projects."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT


TMP = Path(tempfile.mkdtemp(prefix="bug326-host-auth-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "required"

import auth  # noqa: E402
import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.storage.repositories.agent_host_enrollments import (  # noqa: E402
    PERSONAL_EXECUTION_POLICY,
)


SOURCE = "atlas"
TARGET = "switchboard"
UNGRANTED = "maxwell"
HOST = "host/bug326-mac"
PRINCIPAL = "host-bug326"
TOKEN = "bug326-host-token"
PROJECTS = [SOURCE, TARGET, UNGRANTED]


def inventory(projects: list[str]) -> dict:
    local_auth = {
        "available": True,
        "runtime": "codex",
        "provider_credential_exported": False,
    }
    return {
        "host_id": HOST,
        "hostname": "bug326-mac",
        "agent_host_version": "0.4.32",
        "repo_root": str(ROOT),
        "runtimes": [{
            "runtime": "codex",
            "provider": "openai-codex",
            "lanes": [],
            "capabilities": ["docs", "github", "python", "tests"],
            "policy": {"allow_work": True, "allow_global_claim": False},
            "local_auth": local_auth,
        }],
        "limits": {"max_sessions": 8},
        "capacity": {
            "active_sessions": 0,
            "owner": {
                "user_id": "user/steve",
                "tenant_allowlist": ["org-6th-element-labs"],
                "project_allowlist": projects,
                "provider_allowlist": ["openai-codex"],
            },
            "local_auth": local_auth,
        },
        "heartbeat_ttl_s": 180,
    }


try:
    store.init_db(TARGET)
    store.init_db(UNGRANTED)
    created = store.create_project(
        "Atlas", project_id=SOURCE, actor="bug326-test",
        purpose="BUG-326 source", boundary="BUG-326 source boundary")
    assert created.get("created") is True, created
    store.init_db(SOURCE)
    for project in PROJECTS:
        store.set_project_access(
            project, "org-6th-element-labs", owner_user_id="user/steve",
            created_by="bug326-test")
    store.set_project_repo_topology(
        project=SOURCE, canonical_repo="6th-Element-Labs/ActionEngine",
        canonical_default_branch="main")
    store.set_project_repo_topology(
        project=TARGET, canonical_repo="6th-Element-Labs/projectplanner",
        canonical_default_branch="master")

    now = time.time()
    fingerprint = "sha256:" + "b" * 64
    with _conn(SOURCE) as connection:
        connection.execute(
            "INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (PRINCIPAL, "host", HOST, SOURCE,
             json.dumps(["read", "write:agent_host"]), auth.token_hash(TOKEN), now),
        )
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "execution_policy_json,bootstrap_hash,bootstrap_expires_at,bootstrap_consumed_at,"
            "principal_id,public_key_fingerprint,identity_generation,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("hostenroll-bug326", SOURCE, HOST, HOST, "user/steve",
             json.dumps(["org-6th-element-labs"]), json.dumps(PROJECTS),
             json.dumps(["openai-codex"]), json.dumps(PERSONAL_EXECUTION_POLICY),
             "bug326-bootstrap", now + 3600, now, PRINCIPAL, fingerprint,
             1, "active", now, now),
        )

    source_registration = store.register_host(
        inventory(PROJECTS), principal_id=PRINCIPAL, actor=HOST, project=SOURCE)
    assert source_registration.get("host_id") == HOST, source_registration
    grant = store.create_agent_host_project_grant(
        source_project=SOURCE,
        host_id=HOST,
        target_project=TARGET,
        canonical_repository="6th-Element-Labs/projectplanner",
        runtime="codex",
        provider="openai-codex",
        trust_zone="org_shared",
        isolation_mode="worktree",
        max_concurrency=8,
        actor="user/steve",
    )
    assert grant.get("granted") is True, grant

    target_principal = auth.authenticate(
        TARGET, TOKEN, required_scopes=("write:agent_host",))
    assert target_principal.get("id") == PRINCIPAL, target_principal
    assert target_principal.get("authorized_project") == TARGET, target_principal
    assert target_principal.get("agent_host_project_grant", {}).get(
        "target_project_id") == TARGET, target_principal

    target_identity = store.check_agent_host_identity(
        HOST, PRINCIPAL, project=TARGET)
    assert target_identity.get("required") is True, target_identity
    assert target_identity.get("allowed") is True, target_identity
    assert target_identity.get("project_grant", {}).get(
        "target_project_id") == TARGET, target_identity

    target_registration = store.register_host(
        inventory(PROJECTS), principal_id=PRINCIPAL, actor=HOST, project=TARGET)
    assert target_registration.get("host_id") == HOST, target_registration
    assert target_registration.get("authoritative_execution_policy", {}).get(
        "schema") == PERSONAL_EXECUTION_POLICY["schema"], target_registration

    try:
        auth.authenticate(UNGRANTED, TOKEN, required_scopes=("write:agent_host",))
    except PermissionError as exc:
        assert "not valid for this project" in str(exc), exc
    else:
        raise AssertionError("an enrollment allowlist without a Fleet grant must stay denied")

    print("BUG-326 multi-project Agent Host authorization: PASS")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
