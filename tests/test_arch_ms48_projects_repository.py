#!/usr/bin/env python3
"""ARCH-MS-48: projects bootstrap under storage/repositories/projects."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms48-projects-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


for name in (
    "switchboard.storage.repositories.projects",
    "projects_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/projects.py").is_file(),
   "projects.py exists under storage/repositories")
ok((ROOT / "projects_store.py").is_file(),
   "projects_store.py shim exists at repo root")

from switchboard.storage.repositories import projects as projects_repo  # noqa: E402
import projects_store  # noqa: E402
import store  # noqa: E402

ok(projects_store.init_db is projects_repo.init_db,
   "projects_store shim re-exports init_db")
ok(projects_store.create_project is projects_repo.create_project,
   "projects_store shim re-exports create_project")
ok(store.init_db is projects_repo.init_db,
   "store facade delegates init_db to package module")
ok(store.seed_if_empty is projects_repo.seed_if_empty,
   "store facade delegates seed_if_empty to package module")
ok(store.probe_project_db is projects_repo.probe_project_db,
   "store facade delegates probe_project_db to package module")
ok(store.create_project is projects_repo.create_project,
   "store facade delegates create_project to package module")
ok(store.get_project_repo_topology is projects_repo.get_project_repo_topology,
   "store facade delegates get_project_repo_topology to package module")
ok(store.set_project_repo_topology is projects_repo.set_project_repo_topology,
   "store facade delegates set_project_repo_topology to package module")
ok(store.get_project_github_repo is projects_repo.get_project_github_repo,
   "store facade delegates get_project_github_repo to package module")
ok(store.get_project_context is projects_repo.get_project_context,
   "store facade delegates get_project_context to package module")
ok(store.get_session_policy_profiles is projects_repo.get_session_policy_profiles,
   "store facade delegates get_session_policy_profiles to package module")
ok(store.init_db.__module__ == "switchboard.storage.repositories.projects",
   "init_db lives under switchboard.storage.repositories.projects")
ok(store.create_project.__module__ == "switchboard.storage.repositories.projects",
   "create_project lives under switchboard.storage.repositories.projects")
ok(isinstance(store.projects_repository, projects_repo.StoreProjectsRepository),
   "store.projects_repository is StoreProjectsRepository")

shell_src = (ROOT / "src/switchboard/storage/repositories/shell.py").read_text()
proj_src = (ROOT / "src/switchboard/storage/repositories/projects.py").read_text()
ok("def init_db(" not in shell_src, "shell residual no longer defines init_db")
ok("def seed_if_empty(" not in shell_src, "shell residual no longer defines seed_if_empty")
ok("def create_project(" not in shell_src, "shell residual no longer defines create_project")
ok("def get_project_repo_topology(" not in shell_src,
   "shell residual no longer defines get_project_repo_topology")
ok("def get_working_agreement(" in shell_src,
   "get_working_agreement remains in shell residual")
ok("def init_db(" in proj_src, "projects repository owns init_db")
ok("def create_project(" in proj_src, "projects repository owns create_project")
ok(len(proj_src.splitlines()) > 400, "projects extract is substantial")
ok(len(shell_src.splitlines()) < 4800, "shell residual shrunk after ARCH-MS-48 extract")

try:
    store.init_project_registry()
    ok(store.init_db("switchboard") is True, "init_db applies schema for switchboard")
    seeded = store.seed_if_empty("switchboard")
    ok(isinstance(seeded, int) and seeded >= 0, f"seed_if_empty returns count ({seeded})")
    ok(store.probe_project_db("switchboard") is None,
       "probe_project_db reports switchboard ready")
    topo = store.get_project_repo_topology("switchboard")
    ok(bool(topo and topo.get("schema") == "switchboard.project_repo_topology.v1"),
       "get_project_repo_topology returns topology schema")
    ctx = store.get_project_context("switchboard")
    ok(bool(ctx and ctx.get("project") == "switchboard"),
       "get_project_context returns switchboard context")
    created = store.create_project("ms48-arch-proof", actor="arch-ms48")
    ok(bool(created and created.get("created") and not created.get("error")),
       f"create_project succeeds via package SQL ({created.get('error')})")
    pid = (created.get("project") or {}).get("id")
    ok(pid == "ms48-arch-proof", "created project id is ms48-arch-proof")
    ok(store.probe_project_db(pid) is None, "created project db passes readiness probe")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-48 projects repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
