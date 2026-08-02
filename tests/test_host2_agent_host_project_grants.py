#!/usr/bin/env python3
"""HOST-2 shared Agent Host authorization and Fleet ownership proof."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT

TMP = Path(tempfile.mkdtemp(prefix="host2-host-grants-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from switchboard.api.routers.agents import create_router  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def make_project(project: str, repository: str) -> None:
    created = store.create_project(
        project, project_id=project, actor="host2-test",
        purpose=f"{project} tests", boundary=f"{project} boundary")
    assert created.get("created") is True, created
    store.init_db(project)
    store.set_project_repo_topology(
        project=project, canonical_repo=repository, canonical_default_branch="main")
    store.set_project_access(
        project, "org-host2", owner_user_id="user/steve", created_by="host2-test")


try:
    source = "switchboard"
    host_id = "host/steve-existing-mac"
    store.init_db(source)
    store.set_project_access(
        source, "org-host2", owner_user_id="user/steve", created_by="host2-test")
    make_project("simplemark", "StevenRidder/simplemark")
    make_project("atlas", "6th-Element-Labs/atlas")
    now = time.time()
    fingerprint = "sha256:" + "a" * 64
    with _conn(source) as connection:
        connection.execute(
            "INSERT INTO principals(id,kind,display_name,project,scopes,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("principal/steve-host", "agent_host", host_id, source,
             json.dumps(["read", "write:agent_host"]), "hash", now),
        )
        connection.execute(
            "INSERT INTO agent_host_enrollments("
            "enrollment_id,project_id,requested_host_id,host_id,owner_user_id,"
            "tenant_allowlist_json,project_allowlist_json,provider_allowlist_json,"
            "execution_policy_json,bootstrap_hash,bootstrap_expires_at,bootstrap_consumed_at,"
            "principal_id,public_key_fingerprint,identity_generation,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("enroll-steve-mac", source, host_id, host_id, "user/steve",
             json.dumps(["org-host2"]), json.dumps([source]), json.dumps(["openai-codex"]),
             "{}", "bootstrap", now + 60, now, "principal/steve-host", fingerprint,
             4, "active", now, now),
        )
        connection.execute(
            "INSERT INTO agent_hosts(host_id,hostname,agent_host_version,repo_root,"
            "runtimes_json,limits_json,capacity_json,principal_id,registered_at,heartbeat_at,"
            "heartbeat_ttl_s,status,last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (host_id, "Steve's Mac", "1.0", str(ROOT),
             json.dumps([{"runtime": "codex", "local_auth": {"available": True}}]),
             json.dumps({"max_sessions": 8}),
             json.dumps({"active_sessions": 0, "placement": {"host_class": "persistent"}}),
             "principal/steve-host", now, now, 60, "online", ""),
        )

    before = store.get_agent_host_enrollment(host_id, project=source)
    simplemark = store.create_agent_host_project_grant(
        source_project=source, host_id=host_id, target_project="simplemark",
        canonical_repository="StevenRidder/simplemark", runtime="codex",
        provider="openai-codex", trust_zone="org_shared", isolation_mode="worktree",
        max_concurrency=2, actor="user/steve")
    atlas = store.create_agent_host_project_grant(
        source_project=source, host_id=host_id, target_project="atlas",
        canonical_repository="6th-Element-Labs/atlas", runtime="codex",
        provider="openai-codex", trust_zone="org_shared", isolation_mode="worktree",
        max_concurrency=1, actor="user/steve")
    ok(simplemark.get("granted") is True and atlas.get("granted") is True,
       "one existing account-owned Host receives independent project/repository grants")
    after = store.get_agent_host_enrollment(host_id, project=source)
    ok(before["enrollment_id"] == after["enrollment_id"]
       and before["owner_user_id"] == after["owner_user_id"],
       "granting neither duplicates enrollment nor transfers Host ownership")

    projected = store.list_agent_hosts(project="simplemark")
    grant_host = next((host for host in projected if host.get("host_id") == host_id), {})
    placement = (grant_host.get("capacity") or {}).get("placement") or {}
    ok((grant_host.get("shared_grant") or {}).get("eligible") is True
       and placement.get("projects") == ["simplemark"]
       and placement.get("repositories") == ["StevenRidder/simplemark"]
       and grant_host.get("available_sessions") == 2,
       "target project discovers only the repo-scoped grant with bounded concurrency")

    wrong_repo = store.create_agent_host_project_grant(
        source_project=source, host_id=host_id, target_project="simplemark",
        canonical_repository="attacker/wrong", runtime="codex", provider="openai-codex",
        trust_zone="org_shared", isolation_mode="worktree", max_concurrency=1,
        actor="user/steve")
    ok(wrong_repo.get("error_code") == "canonical_repository_mismatch",
       "repository access fails closed with a named canonical mismatch")
    store.set_project_access(
        "atlas", "org-foreign", owner_user_id="user/other", created_by="host2-test")
    foreign = store.create_agent_host_project_grant(
        source_project=source, host_id=host_id, target_project="atlas",
        canonical_repository="6th-Element-Labs/atlas", runtime="codex", provider="openai-codex",
        trust_zone="org_shared", isolation_mode="worktree", max_concurrency=1,
        actor="user/steve")
    ok(foreign.get("error_code") == "target_project_access_denied",
       "a source-project operator cannot grant a Host into an unrelated account or org")
    store.set_project_access(
        "atlas", "org-host2", owner_user_id="user/steve", created_by="host2-test")

    revoked = store.revoke_agent_host_project_grant(
        source_project=source, grant_id=simplemark["grant"]["grant_id"],
        reason="host2 acceptance", actor="user/steve")
    ok(revoked.get("revoked") is True
       and not any(host.get("host_id") == host_id
                   for host in store.list_agent_hosts(project="simplemark")),
       "revocation immediately removes target-project discovery and eligibility")
    active = store.list_agent_host_project_grants(source_project=source)
    ok(any(grant["target_project_id"] == "atlas" for grant in active)
       and not any(grant["target_project_id"] == "simplemark" for grant in active),
       "revocation leaves existing unrelated project grants unchanged")

    with _conn(source) as connection:
        connection.execute(
            "UPDATE agent_hosts SET heartbeat_at=? WHERE host_id=?", (now - 120, host_id))
    stale = store.create_agent_host_project_grant(
        source_project=source, host_id=host_id, target_project="simplemark",
        canonical_repository="StevenRidder/simplemark", runtime="codex",
        provider="openai-codex", trust_zone="org_shared", isolation_mode="worktree",
        max_concurrency=1, actor="user/steve")
    ok(stale.get("error_code") == "host_attestation_stale",
       "stale Host attestation fails closed with a named reason")

    ui = (ROOT / "static/app.js").read_text()
    ok("Authorize project…" in ui and "data-host-grant-revoke" in ui
       and "/ixp/v1/agent-host-grants" in ui,
       "Fleet owns grant and revoke controls on each Host card")

    principal = {"id": "user/steve", "scopes": ["read", "admin", "write:system"],
                 "effective_scopes": ["read", "admin", "write:system"]}
    app = FastAPI()
    app.include_router(create_router(
        resolve_project=lambda project: project,
        resolve_principal=lambda *_args, **_kwargs: principal,
        resolve_body_project=lambda body: str(body.get("project") or ""),
        control_plane_http=lambda result: result,
    ))
    client = TestClient(app)
    listed = client.get(f"/ixp/v1/agent-host-grants?project={source}&include_revoked=true")
    ok(listed.status_code == 200 and len(listed.json().get("grants") or []) == 2,
       "typed Fleet REST read lists active and revoked grants without exposing credentials")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nHOST-2 Agent Host project grants: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
