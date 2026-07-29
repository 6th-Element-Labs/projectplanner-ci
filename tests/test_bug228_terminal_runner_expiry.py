#!/usr/bin/env python3
"""BUG-228: replayed terminal receipts do not renew runner-session expiry.

Runner sessions are Capacity-plane truth under ADR-0008. A host may replay a
terminal observation while acknowledgement side effects converge, but the
replay cannot impersonate a live heartbeat or keep a finished runner visible
forever.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug228-terminal-expiry-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.storage.repositories import runner as runner_repo  # noqa: E402


P = "switchboard"
RUNNER_ID = "run_bug228_terminal_expiry"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


original_time = runner_repo.time.time
try:
    store.init_db(P)

    runner_repo.time.time = lambda: 1_000.0
    store.upsert_runner_session({
        "runner_session_id": RUNNER_ID,
        "host_id": "host/bug228",
        "agent_id": "codex/BUG-228",
        "runtime": "codex",
        "status": "running",
        "heartbeat_ttl_s": 180,
    }, actor="bug228-test", project=P)

    runner_repo.time.time = lambda: 1_100.0
    first_terminal = store.upsert_runner_session({
        "runner_session_id": RUNNER_ID,
        "status": "exited",
        "metadata": {"terminalized_by": "host_supervisor"},
    }, actor="bug228-test", project=P)
    ok(first_terminal.get("heartbeat_at") == 1_100.0,
       "the first live-to-terminal transition records its observation time")
    ok(first_terminal.get("expires_at") == 1_280.0,
       "the terminal receipt keeps one bounded display window")

    runner_repo.time.time = lambda: 1_200.0
    replayed = store.upsert_runner_session({
        "runner_session_id": RUNNER_ID,
        "status": "exited",
        "metadata": {"terminalized_by": "host_supervisor"},
    }, actor="bug228-test", project=P)
    ok(replayed.get("heartbeat_at") == 1_100.0,
       "a repeated terminal receipt preserves the first terminal timestamp")
    ok(replayed.get("expires_at") == 1_280.0,
       "a repeated terminal receipt cannot renew terminal expiry")
    ok(replayed.get("stale") is False,
       "the terminal receipt remains observable during its bounded window")

    runner_repo.time.time = lambda: 1_281.0
    visible = store.list_runner_sessions(
        host_id="host/bug228", include_stale=False, project=P)
    retained = store.list_runner_sessions(
        host_id="host/bug228", include_stale=True, project=P)
    ok(visible == [],
       "the normal runner feed drops the terminal row after its original TTL")
    ok(len(retained) == 1 and retained[0].get("stale") is True,
       "the expired terminal observation remains auditable as stale history")
finally:
    runner_repo.time.time = original_time
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nBUG-228 terminal runner expiry: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
