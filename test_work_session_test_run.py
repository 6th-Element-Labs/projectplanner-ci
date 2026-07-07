#!/usr/bin/env python3
"""SESSION-10 executed test-run helper regressions."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "work_session_test_run.py"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


with tempfile.TemporaryDirectory(prefix="work-session-test-run-") as tmp:
    log_path = os.path.join(tmp, "run.log")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cwd",
            tmp,
            "--work-session-id",
            "worksession-test",
            "--task-id",
            "SESSION-10",
            "--claim-id",
            "claim-test",
            "--agent-id",
            "codex/SESSION-10",
            "--branch",
            "codex/SESSION-10-executed-tests",
            "--head-sha",
            "abc123",
            "--log-path",
            log_path,
            "--command",
            f"{sys.executable} -c \"print('session10-ok')\"",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ok(proc.returncode == 0, "helper returns command exit status")
    evidence = json.loads(proc.stdout)
    ok(evidence["schema"] == "switchboard.executed_test_run.v1",
       "helper emits executed test-run schema")
    ok(evidence["status"] == "success" and evidence["exit_code"] == 0,
       "helper records success and exit code")
    ok(evidence["work_session_id"] == "worksession-test" and
       evidence["branch"] == "codex/SESSION-10-executed-tests",
       "helper preserves session binding fields")
    ok(evidence["output_hash"].startswith("sha256:") and len(evidence["output_hash"]) == 71,
       "helper records durable output hash")
    ok(os.path.exists(log_path) and "session10-ok" in open(log_path, encoding="utf-8").read(),
       "helper writes optional log artifact")

with tempfile.TemporaryDirectory(prefix="work-session-test-run-fail-") as tmp:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cwd",
            tmp,
            "--command",
            f"{sys.executable} -c \"import sys; sys.exit(7)\"",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    evidence = json.loads(proc.stdout)
    ok(proc.returncode == 7 and evidence["status"] == "failed",
       "helper preserves failing command status")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
