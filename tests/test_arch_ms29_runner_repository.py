#!/usr/bin/env python3
"""ARCH-MS-29: runner persistence under switchboard.storage.repositories.runner."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms29-runner-")
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
    "switchboard.storage.repositories.runner",
    "runner_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/runner.py").is_file(),
   "runner.py exists under storage/repositories")

from switchboard.storage.repositories import runner as runner_repo  # noqa: E402
import runner_store  # noqa: E402
import store  # noqa: E402

ok(runner_store.upsert_runner_session is runner_repo.upsert_runner_session,
   "runner_store shim re-exports package upsert_runner_session")
ok(runner_store.request_runner_control is runner_repo.request_runner_control,
   "runner_store shim re-exports package request_runner_control")
ok(store.upsert_runner_session is runner_repo.upsert_runner_session,
   "store facade delegates runner upsert to package module")
ok(store.get_runner_session is runner_repo.get_runner_session,
   "store facade delegates runner get to package module")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    now = time.time()
    created = store.upsert_runner_session({
        "runner_session_id": "arch-ms29-runner",
        "host_id": "host/ms29",
        "agent_id": "cursor/ms29",
        "runtime": "cursor",
        "status": "running",
        "heartbeat_at": now,
        "heartbeat_ttl_s": 120,
        "control": {"managed_process": True},
    }, actor="arch-ms29", project="switchboard")
    ok(created.get("runner_session_id") == "arch-ms29-runner",
       "upsert_runner_session persists a runner session")
    ok("health" in (created.get("available_actions") or []),
       "managed runner session advertises health action")

    bad = store.request_runner_control(
        "arch-ms29-runner", "not-a-real-action", project="switchboard")
    ok(bad.get("requested") is False and bad.get("error") == "unsupported_action",
       "unsupported runner control action fails closed")

    fetched = store.get_runner_session("arch-ms29-runner", project="switchboard")
    ok(fetched is not None and fetched.get("host_id") == "host/ms29",
       "get_runner_session reads persisted runner session")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-29 runner repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
