#!/usr/bin/env python3
"""Smoke test for the Codex managed process supervisor."""
import importlib.util
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUPERVISOR = ROOT / "adapters" / "codex" / "supervisor.py"
spec = importlib.util.spec_from_file_location("codex_supervisor", SUPERVISOR)
supervisor = importlib.util.module_from_spec(spec)
sys.modules["codex_supervisor"] = supervisor
spec.loader.exec_module(supervisor)

_TMP = tempfile.mkdtemp(prefix="codex-supervisor-")
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    command = [
        sys.executable,
        "-c",
        "import os,time; print(os.environ['PM_RUNNER_SESSION_ID'], flush=True); time.sleep(30)",
    ]
    started = supervisor.start_session(
        command,
        agent_id="codex/supervisor-test",
        task_id="ADAPTER-8",
        runner_dir=_TMP,
        cwd=str(ROOT),
    )
    ok(started["runner_session_id"].startswith("run_"), "start_session creates runner_session_id")
    ok(started["status"] == "running" and started["alive"], "start_session launches child")
    status = supervisor.status_session(started["runner_session_id"], _TMP)
    ok(status["pid"] == started["pid"] and status["alive"], "status_session reports live child")
    deadline = time.time() + 2
    while time.time() < deadline:
        if started["runner_session_id"] in Path(started["log_path"]).read_text(errors="replace"):
            break
        time.sleep(0.05)
    snap = supervisor.snapshot_session(started["runner_session_id"], _TMP)
    ok(snap["last_snapshot"]["runner_session_id"] == started["runner_session_id"],
       "snapshot_session records a live snapshot")
    ok(started["runner_session_id"] in snap["last_snapshot"]["log_tail"],
       "snapshot_session preserves child log tail")
    time.sleep(0.2)
    killed = supervisor.kill_session(started["runner_session_id"], _TMP, grace_seconds=0.1)
    ok(killed["status"] == "killed" and not killed["alive"], "kill_session terminates child")
    ok(killed["last_snapshot"]["runner_session_id"] == started["runner_session_id"],
       "kill_session records pre-kill snapshot")
    ok(started["runner_session_id"] in killed["last_snapshot"]["log_tail"],
       "snapshot preserves child log tail")
    listed = supervisor.list_sessions(_TMP)
    ok(listed and listed[0]["runner_session_id"] == started["runner_session_id"],
       "list_sessions returns persisted session")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
