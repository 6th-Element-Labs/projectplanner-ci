#!/usr/bin/env python3
"""ENFORCE-23: repo-topology list fields support an explicit clear."""
from __future__ import annotations

import os
import json
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401


TMP = tempfile.mkdtemp(prefix="enforce23-topology-clear-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
import mcp_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import app  # noqa: E402
from switchboard.application.commands.merge_gate import _merge_gate_required_contexts  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


PROJECT = "enforce23-clear-proof"

try:
    store.init_project_registry()
    created = store.create_project(PROJECT, project_id=PROJECT, actor="enforce23-test")
    ok(created.get("created") is True, "created isolated project")
    store.init_db(PROJECT)

    seeded = store.set_project_repo_topology(
        project=PROJECT,
        canonical_repo="6th-Element-Labs/projectplanner",
        public_ci_repo="6th-Element-Labs/projectplanner-ci",
        public_ci_required_status_contexts=["legacy-ci"],
    )
    ok((seeded.get("repo_topology", {}).get("roles", {}).get("public_ci", {})
        .get("required_status_contexts")) == ["legacy-ci"],
       "seeded public-CI required context")

    cleared = store.set_project_repo_topology(
        project=PROJECT, public_ci_required_status_contexts=[])
    topology = cleared.get("repo_topology", {})
    public_ci = (topology.get("roles") or {}).get("public_ci") or {}
    ok(public_ci.get("required_status_contexts") == [],
       "explicit empty list clears the stored public-CI context")
    ok(_merge_gate_required_contexts(topology, {"required_status_contexts": ["gate"]}) == ["gate"],
       "merge gate now requires only the canonical GitHub gate")

    store.set_project_repo_topology(
        project=PROJECT, public_ci_required_status_contexts=["legacy-ci"])
    rest = TestClient(app).post(
        f"/api/projects/{PROJECT}/repo_topology",
        json={"public_ci_required_status_contexts": []},
    )
    rest_public_ci = (((rest.json().get("repo_topology") or {}).get("roles") or {})
                      .get("public_ci") or {})
    ok(rest.status_code == 200 and rest_public_ci.get("required_status_contexts") == [],
       "REST preserves an explicit empty list instead of dropping it")

    store.set_project_repo_topology(
        project=PROJECT, public_ci_required_status_contexts=["legacy-ci"])
    mcp_cleared = json.loads(mcp_server.set_project_repo_topology(
        None, project=PROJECT, public_ci_required_status_contexts="[]"))
    mcp_public_ci = (((mcp_cleared.get("repo_topology") or {}).get("roles") or {})
                     .get("public_ci") or {})
    ok(mcp_public_ci.get("required_status_contexts") == [],
       "MCP preserves an explicit empty list instead of treating it as omitted")

    mcp_untouched = json.loads(mcp_server.set_project_repo_topology(None, project=PROJECT))
    ok((((mcp_untouched.get("repo_topology") or {}).get("roles") or {}).get("public_ci") or {})
       .get("required_status_contexts") == [],
       "MCP omission preserves the cleared value")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nENFORCE-23 repo topology clear: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
