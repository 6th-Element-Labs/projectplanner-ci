#!/usr/bin/env python3
"""ACCESS-27: project execution policy as the runner authority.

Covers the acceptance set: valid/invalid fixtures, authorized round-trip,
cross-project denial, boot projection, configuration-only new-project setup, and
a typed readiness failure whenever policy is missing or invalid.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401 — makes repo-root modules importable


TMP = tempfile.mkdtemp(prefix="access27-execution-policy-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_TOP_LEVEL_PROJECTS"] = "maxwell,helm,switchboard"

import db.connection as db_connection  # noqa: E402
import project_contract  # noqa: E402
import store  # noqa: E402
from constants import PROJECT_EXECUTION_POLICY_SCHEMA  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


VALID_POLICY = {
    "runtimes": {"allowed": ["claude_code", "codex"], "default": "claude_code"},
    "workspace": {"repo_role": "canonical", "isolation": "worktree"},
    "placement": {
        "host_classes": ["personal", "ephemeral"],
        "trust_zones": ["personal", "cloud_ephemeral"],
        "burst": {"enabled": True, "max_concurrent_ephemeral": 4},
    },
    "providers": {"selectors": [
        {"provider": "anthropic", "connection_reference": "providerconn-abc",
         "account_affinity_id": "affinity-1", "priority": 0},
    ]},
    "scm": {"provider": "github", "connection_reference": "scmconn-xyz"},
    "autopilot": {"enabled": True, "profile_id": "autopilot-default"},
    "lifecycle": {"status": "active"},
}


def make_project(project_id: str, canonical_repo: str) -> None:
    created = store.create_project(
        project_id, project_id=project_id, actor="fixture",
        purpose=f"{project_id} purpose", boundary=f"{project_id} boundary")
    assert created.get("created") is True, created
    store.init_db(project_id)
    store.set_project_repo_topology(
        project=project_id, canonical_repo=canonical_repo,
        canonical_default_branch="main")


try:
    store.init_db("switchboard")

    # --- typed readiness failure: nothing configured ------------------------
    make_project("exec-alpha", "acme/alpha")
    unset = store.get_project_execution_policy("exec-alpha")
    readiness = unset["readiness"]
    ok(unset["schema"] == PROJECT_EXECUTION_POLICY_SCHEMA
       and unset["configured"] is False and unset["valid"] is False,
       "an unconfigured project reports schema, configured=False, and valid=False")
    ok(readiness["passed"] is False and readiness["status"] == "blocked"
       and readiness["reason_code"] == "project_execution_policy_missing",
       "missing policy yields a typed blocked readiness gate, not an optimistic pass")

    # --- invalid fixtures are rejected and persist nothing -------------------
    invalid_cases = [
        ("unknown runtime",
         {**VALID_POLICY, "runtimes": {"allowed": ["rogue_cli"], "default": "rogue_cli"}}),
        ("default runtime outside the allowlist",
         {**VALID_POLICY, "runtimes": {"allowed": ["codex"], "default": "claude_code"}}),
        ("unknown isolation mode",
         {**VALID_POLICY,
          "workspace": {"repo_role": "canonical", "isolation": "bare_host"}}),
        ("unknown trust zone",
         {**VALID_POLICY, "placement": {**VALID_POLICY["placement"],
                                        "trust_zones": ["anywhere"]}}),
        ("burst without the ephemeral host class",
         {**VALID_POLICY, "placement": {
             "host_classes": ["personal"], "trust_zones": ["personal"],
             "burst": {"enabled": True, "max_concurrent_ephemeral": 2}}}),
        ("provider selector without a connection reference",
         {**VALID_POLICY, "providers": {"selectors": [{"provider": "anthropic"}]}}),
        ("Autopilot enabled without a profile id",
         {**VALID_POLICY, "autopilot": {"enabled": True, "profile_id": ""}}),
        ("repo role this project has not configured",
         {**VALID_POLICY,
          "workspace": {"repo_role": "release", "isolation": "worktree"}}),
    ]
    for label, fixture in invalid_cases:
        result = store.set_project_execution_policy(
            project="exec-alpha", updates=fixture, actor="fixture")
        ok(result.get("error") == "project_execution_policy_invalid"
           and bool(result.get("invalid")),
           f"invalid fixture rejected with a typed error: {label}")
    ok(store.get_project_execution_policy("exec-alpha")["configured"] is False,
       "a rejected update persists nothing — the project stays unconfigured")

    # --- references and policy only ----------------------------------------
    for forbidden in ({"workspace": {"branch": "feature/x"}},
                      {"providers": {"env": {"ANTHROPIC_API_KEY": "sk-live"}}},
                      {"scm": {"token": "ghp_live"}}):
        forbidden_result = store.set_project_execution_policy(
            project="exec-alpha", updates={**VALID_POLICY, **forbidden}, actor="fixture")
        ok(forbidden_result.get("error") == "project_execution_policy_forbidden_field",
           f"branch/env/secret configuration is rejected: {sorted(forbidden)[0]}")

    # --- authorized round-trip ---------------------------------------------
    written = store.set_project_execution_policy(
        project="exec-alpha", updates=VALID_POLICY, actor="fixture")
    stored = written.get("execution_policy") or {}
    ok(not written.get("error") and stored.get("valid") is True
       and stored["readiness"]["passed"] is True
       and stored["readiness"]["reason_code"] == "",
       "a valid policy round-trips and flips readiness to passed")
    ok(stored["runtimes"] == {"allowed": ["claude_code", "codex"],
                              "default": "claude_code"}
       and stored["workspace"] == {"repo_role": "canonical", "isolation": "worktree"}
       and stored["placement"]["host_classes"] == ["personal", "ephemeral"]
       and stored["placement"]["burst"] == {"enabled": True,
                                            "max_concurrent_ephemeral": 4}
       and stored["providers"]["selectors"][0]["connection_reference"] == "providerconn-abc"
       and stored["scm"] == {"provider": "github",
                             "connection_reference": "scmconn-xyz"}
       and stored["autopilot"] == {"enabled": True,
                                   "profile_id": "autopilot-default"},
       "runtimes, workspace, placement, providers, SCM, and Autopilot all persist")
    ok(stored["lifecycle"]["revision"] == 1
       and stored["lifecycle"]["updated_by"] == "fixture"
       and stored["lifecycle"]["created_at"] is not None,
       "lifecycle records revision, author, and creation time")

    # --- lifecycle/versioning on a partial update ---------------------------
    merged = store.set_project_execution_policy(
        project="exec-alpha", updates={"autopilot": {"enabled": False}},
        actor="operator")["execution_policy"]
    ok(merged["lifecycle"]["revision"] == 2
       and merged["autopilot"]["enabled"] is False
       and merged["runtimes"]["default"] == "claude_code",
       "a partial update merges, bumps the revision, and leaves other policy intact")

    retired = store.set_project_execution_policy(
        project="exec-alpha", updates={"lifecycle": {"status": "retired"}},
        actor="operator")["execution_policy"]
    ok(retired["readiness"]["passed"] is False
       and retired["readiness"]["reason_code"] == "project_execution_policy_not_active",
       "a retired policy blocks readiness with its own typed reason code")
    store.set_project_execution_policy(
        project="exec-alpha", updates={"lifecycle": {"status": "active"}},
        actor="operator")

    # --- boot projection ----------------------------------------------------
    context = store.get_project_context("exec-alpha")
    ok((context.get("execution_policy") or {}).get("schema")
       == PROJECT_EXECUTION_POLICY_SCHEMA
       and (context.get("execution_readiness") or {}).get("passed") is True,
       "get_project_context projects the policy and its readiness gate")
    contract = project_contract.build("exec-alpha")
    ok((contract.get("execution_policy") or {}).get("valid") is True
       and (contract.get("execution_readiness") or {}).get("name")
       == "project_execution_policy_ready"
       and any("execution_policy" in rule for rule in contract["operating_rules"]),
       "the boot project contract carries the policy, readiness, and operating rule")

    # --- configuration-only setup for a brand-new project -------------------
    make_project("exec-beta", "acme/beta")
    beta_blocked = project_contract.build("exec-beta")["execution_readiness"]
    beta = store.set_project_execution_policy(
        project="exec-beta", updates=VALID_POLICY, actor="fixture")["execution_policy"]
    ok(beta_blocked["reason_code"] == "project_execution_policy_missing"
       and beta["valid"] is True and beta["project"] == "exec-beta",
       "a brand-new project becomes execution-ready by configuration alone")
    ok(store.get_project_execution_policy("exec-alpha")["lifecycle"]["revision"] == 4
       and store.get_project_execution_policy("exec-beta")["lifecycle"]["revision"] == 1,
       "policies are per-project; writing one project never touches another")

    # --- MCP + REST surfaces, and cross-project denial ----------------------
    import mcp_server  # noqa: E402

    mcp_read = json.loads(mcp_server.get_project_execution_policy(
        None, project="exec-beta"))
    ok(mcp_read.get("schema") == PROJECT_EXECUTION_POLICY_SCHEMA
       and mcp_read["readiness"]["passed"] is True,
       "MCP get_project_execution_policy returns the versioned contract")
    mcp_write = json.loads(mcp_server.set_project_execution_policy(
        None, project="exec-beta",
        policy_json=json.dumps({"autopilot": {"enabled": False}})))
    ok((mcp_write.get("execution_policy") or {}).get(
        "autopilot", {}).get("enabled") is False,
       "MCP set_project_execution_policy performs an authorized write")
    mcp_bad_json = json.loads(mcp_server.set_project_execution_policy(
        None, project="exec-beta", policy_json="{not json"))
    mcp_unknown = json.loads(mcp_server.get_project_execution_policy(
        None, project="no-such-project"))
    ok("policy_json must be valid JSON" in str(mcp_bad_json.get("error"))
       and str(mcp_unknown.get("error")).startswith("unknown project"),
       "malformed bodies and unknown projects fail closed on the MCP surface")

    from switchboard.mcp import authorization  # noqa: E402

    ok(authorization.declaration_for("get_project_execution_policy").access_class.value
       == "read"
       and authorization.declaration_for(
           "set_project_execution_policy").access_class.value == "write",
       "both tools are declared in the MCP authorization census")

    from fastapi.testclient import TestClient  # noqa: E402

    import app  # noqa: E402

    client = TestClient(app.app)
    rest_get = client.get("/api/projects/exec-beta/execution_policy")
    rest_post = client.post("/api/projects/exec-beta/execution_policy",
                            json={"autopilot": {"enabled": True,
                                                "profile_id": "autopilot-default"}})
    ok(rest_get.status_code == 200
       and rest_get.json()["schema"] == PROJECT_EXECUTION_POLICY_SCHEMA
       and rest_post.status_code == 200
       and rest_post.json()["execution_policy"]["autopilot"]["enabled"] is True,
       "REST exposes the policy for read and authorized write")
    rest_invalid = client.post(
        "/api/projects/exec-beta/execution_policy",
        json={"runtimes": {"allowed": ["rogue_cli"], "default": "rogue_cli"}})
    ok(rest_invalid.status_code == 400,
       "REST rejects an invalid policy update with 400")

    from switchboard.api import middleware  # noqa: E402

    ok(middleware._write_required_scopes(
        "/api/projects/exec-beta/execution_policy") == ("write:system",),
       "the execution-policy write route requires write:system, not write:tasks")

    # Cross-project denial: a principal bound to one project cannot reach another
    # project's execution policy through the shared MCP authorization gate.
    beta_principal = {"id": "agent-exec-beta", "project": "exec-beta",
                      "kind": "agent", "scopes": ["read", "write:system"]}
    own_project = authorization.authorize_project_context(
        beta_principal, "exec-beta",
        authorization.declaration_for("set_project_execution_policy").access_class)
    denied = None
    try:
        authorization.authorize_project_context(
            beta_principal, "exec-alpha",
            authorization.declaration_for("set_project_execution_policy").access_class)
    except PermissionError as exc:
        denied = str(exc)
    ok(own_project.project_id == "exec-beta" and denied is not None
       and "not valid for this project" in denied,
       "a principal bound to exec-beta may write its own policy but not exec-alpha's")

finally:
    db_connection._close_pooled_conns()
    shutil.rmtree(TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
