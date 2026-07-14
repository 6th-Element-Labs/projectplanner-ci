#!/usr/bin/env python3
"""ARCH-MS-9: ``test_arch_ms0_scaffold`` — Phase 0 platform scaffold proof gate.

ADR-0009 names this the Phase 0 proof artifact. It locks the milestone 0.2 scaffold
(ARCH-MS-7 package skeleton + ARCH-MS-8 create-task application command) against
regression by asserting the three things the charter calls out:

  1. the ``src/switchboard/`` package and its skeleton subpackages import cleanly;
  2. the ``create_task`` application command is callable through the store facade;
  3. both the REST and MCP task adapters invoke the same
     application handler (``switchboard.application.commands.create_task``).

Kept script-style and hermetic so ``scripts/switchboard_ci.sh`` discovers and runs it
with no pytest dependency, matching the rest of the suite.
"""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms0-scaffold-")
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


# --- Assertion 1: the package skeleton imports -------------------------------
# The ADR-0009 target tree. `settings` is a module; the rest are packages that must
# ship an __init__.py so extractions can land on-touch without re-scaffolding.
SKELETON_MODULES = (
    "switchboard.settings",
    "switchboard.storage.migrations.runner",
    "switchboard.storage.repositories.access",
    "switchboard.storage.repositories.claims",
    "switchboard.storage.repositories.coordination",
    "switchboard.storage.repositories.deliverables",
    "switchboard.storage.repositories.provenance",
    "switchboard.storage.repositories.runner",
    "switchboard.storage.repositories.tasks",
)
SKELETON_PACKAGES = (
    "switchboard",
    "switchboard.api",
    "switchboard.api.routers",
    "switchboard.application",
    "switchboard.application.commands",
    "switchboard.application.contracts",
    "switchboard.application.queries",
    "switchboard.contracts",
    "switchboard.contracts.tasks",
    "switchboard.domain",
    "switchboard.domain.access",
    "switchboard.domain.board",
    "switchboard.domain.coordination",
    "switchboard.domain.deliverables",
    "switchboard.domain.provenance",
    "switchboard.integrations",
    "switchboard.mcp",
    "switchboard.mcp.tools",
    "switchboard.storage",
    "switchboard.storage.migrations",
    "switchboard.storage.repositories",
    "switchboard.storage.repositories.protocols",
)

import_failures = []
for name in SKELETON_PACKAGES + SKELETON_MODULES:
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - the failure detail is the signal
        import_failures.append(f"{name}: {exc!r}")
ok(
    not import_failures,
    "src/switchboard/ package skeleton imports cleanly"
    + ("" if not import_failures else " — " + "; ".join(import_failures)),
)

SRC = ROOT / "src"
missing_init = [
    name
    for name in SKELETON_PACKAGES
    if not (SRC / Path(name.replace(".", "/")) / "__init__.py").is_file()
]
ok(
    not missing_init,
    "every skeleton package ships an __init__.py on disk"
    + ("" if not missing_init else " — missing: " + ", ".join(missing_init)),
)

from switchboard.settings import Settings  # noqa: E402

settings = Settings.from_env()
ok(
    settings.auth_mode == "dev-open",
    "switchboard.settings.Settings.from_env() reads the environment",
)

# --- Assertion 2 + 3: create_task callable + adapters share the handler ------
import store  # noqa: E402
from switchboard.application.commands import create_task  # noqa: E402
from switchboard.application.contracts.tasks import CreateTaskCommand  # noqa: E402

try:
    store.init_project_registry()
    store.init_db("switchboard")

    command = CreateTaskCommand.from_mapping(
        {"workstream_id": "ARCH", "title": "scaffold proof task", "risk_level": "Low"}
    )
    created = create_task.execute(command, actor="arch-ms0", project="switchboard")
    ok(
        isinstance(created, dict) and bool(created.get("task_id")),
        "create_task application command is callable and persists a task",
    )

    before = len(store.list_tasks(project="switchboard"))
    try:
        create_task.execute(
            CreateTaskCommand.from_mapping(
                {"workstream_id": "ARCH", "title": "must not persist",
                 "depends_on": "DOES-NOT-EXIST"}
            ),
            actor="arch-ms0",
            project="switchboard",
        )
        rejected = False
    except create_task.CreateTaskError as exc:
        rejected = exc.code == "unknown_dependencies"
    ok(
        rejected and len(store.list_tasks(project="switchboard")) == before,
        "create_task fails closed on unknown dependencies before any write",
    )

    app_source = entrypoint_source("app")
    task_router_source = (
        ROOT / "src/switchboard/api/routers/tasks.py"
    ).read_text(encoding="utf-8")
    mcp_source = (ROOT / "src/switchboard/mcp/tools/tasks.py").read_text(encoding="utf-8")
    shared_import = (
        "from switchboard.application.commands import create_task as create_task_command"
    )
    rest_wired = (
        "_create_task_router" in app_source
        and shared_import in task_router_source
        and "create_task_command.execute_mapping_result" in task_router_source
    )
    mcp_wired = shared_import in mcp_source and "create_task_command.execute_mapping_result" in mcp_source
    ok(rest_wired, "REST task router invokes the shared create_task handler")
    ok(mcp_wired, "MCP task adapter invokes the shared create_task handler")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
