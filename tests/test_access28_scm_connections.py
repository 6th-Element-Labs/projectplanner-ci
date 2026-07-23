#!/usr/bin/env python3
"""ACCESS-28: SCM installations are project/repository scoped and secret-free."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import path_setup  # noqa: F401

TMP = tempfile.mkdtemp(prefix="access28-scm-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP

from switchboard.storage.repositories.scm_connections import (  # noqa: E402
    SCMConnectionError,
    SCMConnectionRepository,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def topology(project):
    repos = {
        "switchboard": "6th-Element-Labs/projectplanner",
        "helm": "6th-Element-Labs/helm",
    }
    repo = repos.get(project, "")
    return {
        "valid": bool(repo),
        "roles": {"canonical": {"repo": repo}},
    }


repository = SCMConnectionRepository(topology)

try:
    connection = repository.create({
        "connection_id": "scm-access28",
        "project": "switchboard",
        "provider": "github_app",
        "installation_ref": "github-app-installation:opaque-123",
        "org_allowlist": ["6th-Element-Labs"],
        "project_allowlist": ["switchboard"],
        "repository_allowlist": ["6th-Element-Labs/projectplanner"],
        "operation_scopes": ["clone", "push"],
    }, actor="access28-test")
    ok(connection["schema"] == "switchboard.scm_connection.v1",
       "creates the versioned SCM connection contract")
    ok(connection["installation_version"] == 1
       and connection["lifecycle_state"] == "active",
       "persists opaque installation lifecycle metadata")

    raw_token_denied = False
    try:
        repository.create({
            "installation_ref": "opaque",
            "token": "must-never-persist",
            "org_allowlist": ["6th-Element-Labs"],
            "project_allowlist": ["switchboard"],
            "repository_allowlist": ["6th-Element-Labs/projectplanner"],
            "operation_scopes": ["clone"],
        }, actor="access28-test")
    except SCMConnectionError as exc:
        raw_token_denied = exc.code == "raw_scm_credential_rejected"
    ok(raw_token_denied, "rejects raw GitHub tokens at the storage boundary")

    disguised_token_denied = False
    try:
        repository.rotate(
            "scm-access28", "ghs_not-an-opaque-reference", actor="access28-test")
    except SCMConnectionError as exc:
        disguised_token_denied = exc.code == "raw_scm_credential_rejected"
    ok(disguised_token_denied,
       "rejects token-shaped material passed as an installation reference")

    cross_project_admin_denied = False
    try:
        repository.rotate(
            "scm-access28", "github-app-installation:cross-project",
            actor="access28-test", project="helm")
    except SCMConnectionError as exc:
        cross_project_admin_denied = exc.code == "repository_not_authorized"
    ok(cross_project_admin_denied,
       "denies cross-project administration by exact project allowlist")

    clone = repository.preflight(
        "scm-access28", project="switchboard",
        repository="6th-Element-Labs/projectplanner", operation="clone")
    ok(clone["allowed"] is True and clone["installation_ref"].endswith("opaque-123"),
       "returns the opaque installation reference only after exact preflight")

    wrong_repo = repository.preflight(
        "scm-access28", project="switchboard",
        repository="6th-Element-Labs/other", operation="clone")
    wrong_project = repository.preflight(
        "scm-access28", project="helm",
        repository="6th-Element-Labs/projectplanner", operation="clone")
    wrong_scope = repository.preflight(
        "scm-access28", project="switchboard",
        repository="6th-Element-Labs/projectplanner", operation="merge")
    ok(all(item.get("error") == "repository_not_authorized"
           and item["allowed"] is False
           and item["installation_ref"] == ""
           for item in (wrong_repo, wrong_project, wrong_scope)),
       "fails closed with repository_not_authorized before SCM use")

    rotated = repository.rotate(
        "scm-access28", "github-app-installation:opaque-456", actor="access28-test")
    ok(rotated["installation_version"] == 2
       and rotated["installation_ref"].endswith("opaque-456"),
       "rotates the installation reference and increments its version")

    updated = repository.update("scm-access28", {
        "operation_scopes": ["clone", "fetch", "push"],
    }, actor="access28-test")
    ok(updated["operation_scopes"] == ["clone", "fetch", "push"],
       "updates operation scope policy")

    revoked = repository.revoke(
        "scm-access28", "installation removed", actor="access28-test")
    denied_after_revoke = repository.preflight(
        "scm-access28", project="switchboard",
        repository="6th-Element-Labs/projectplanner", operation="push")
    ok(revoked["lifecycle_state"] == "revoked"
       and denied_after_revoke["error"] == "repository_not_authorized",
       "revocation immediately fences clone and push authorization")

    detail = repository.get("scm-access28", include_events=True)
    serialized = json.dumps(detail, sort_keys=True)
    ok("must-never-persist" not in serialized
       and {"created", "rotated", "updated", "revoked", "preflight_denied"}.issubset(
           {event["event_type"] for event in detail["events"]}),
       "audit history is durable without raw token material")

    deleted = repository.delete(
        "scm-access28", "retired", actor="access28-test")
    missing = False
    try:
        repository.get("scm-access28")
    except SCMConnectionError as exc:
        missing = exc.code == "scm_connection_not_found"
    ok(deleted["deleted"] is True and missing,
       "deletion tombstones and erases the installation reference")
finally:
    print(f"\nACCESS-28 SCM connections: {passed} passed, {failed} failed")

raise SystemExit(1 if failed else 0)
