#!/usr/bin/env python3
"""ARCH-MS-30: principal/auth persistence under switchboard.storage.repositories.access."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms30-access-")
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
    "switchboard.storage.repositories.access",
    "auth_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/access.py").is_file(),
   "access.py exists under storage/repositories")
ok((ROOT / "auth_store.py").is_file(),
   "auth_store.py shim exists at repo root")

from switchboard.storage.repositories import access as access_repo  # noqa: E402
import auth_store  # noqa: E402
import store  # noqa: E402

ok(auth_store.create_principal is access_repo.create_principal,
   "auth_store shim re-exports package create_principal")
ok(auth_store.resolve_write_actor is access_repo.resolve_write_actor,
   "auth_store shim re-exports package resolve_write_actor")
ok(store.create_principal is access_repo.create_principal,
   "store facade delegates create_principal to package module")
ok(store.resolve_write_actor is access_repo.resolve_write_actor,
   "store facade delegates resolve_write_actor to package module")
ok(store._identity_takeover_risk_in is access_repo._identity_takeover_risk_in,
   "store facade delegates identity risk helper to package module")
ok(store.create_principal.__module__ == "switchboard.storage.repositories.access",
   "create_principal lives under switchboard.storage.repositories.access")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    created = store.create_principal(
        kind="agent",
        display_name="cursor/ms30",
        token=f"arch-ms30-token-{time.time()}",
        scopes=["read", "write:tasks"],
        project="switchboard",
    )
    ok(created.get("id") and created.get("kind") == "agent",
       "create_principal persists a principal")
    fetched = store.get_principal_by_id(created["id"], project="switchboard")
    ok(fetched is not None and fetched.get("display_name") == "cursor/ms30",
       "get_principal_by_id reads persisted principal")
    public = store.public_principal_record(fetched, project="switchboard")
    ok(public.get("id") == created["id"] and "effective_scopes" in public,
       "public_principal_record shapes principal for API/MCP")

    binding = store.resolve_write_actor("cursor/ms30", project="switchboard")
    ok(binding.get("ok") is True and binding.get("binding") == "principal",
       "resolve_write_actor binds ordinary principal actors")

    unbound = store.resolve_write_actor(
        "env-mcp-token", project="switchboard", task_id="ARCH-MS-30")
    ok(unbound.get("ok") is False and unbound.get("failure_class") == "unbound_identity",
       "resolve_write_actor fails closed for unbound shared tokens")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-30 access repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
