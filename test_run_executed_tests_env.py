#!/usr/bin/env python3
"""COORD-29: managed executed tests inherit the active Python environment."""
import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "switchboard_core_test_env", ROOT / "adapters" / "switchboard_core.py")
sb = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sb
spec.loader.exec_module(sb)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


captured = {}
original_run = sb.subprocess.run
original_path = os.environ.get("PATH", "")


def fake_run(argv, **kwargs):
    captured["argv"] = argv
    captured["env"] = kwargs.get("env")
    evidence = {
        "schema": "switchboard.executed_test_run.v1",
        "status": "success",
        "exit_code": 0,
    }
    return type("Completed", (), {"stdout": json.dumps(evidence)})()


try:
    sb.subprocess.run = fake_run
    result = sb.run_executed_tests(
        "/worktree", "worksession-coord29", "COORD-29", "claim-coord29",
        "codex/COORD-29", commands=["scripts/switchboard_ci.sh"])
finally:
    sb.subprocess.run = original_run

runner_path = (captured.get("env") or {}).get("PATH", "")
interpreter_bin = os.path.dirname(os.path.abspath(sys.executable))
ok(runner_path.split(os.pathsep)[0] == interpreter_bin,
   "executed-test runner PATH starts with the active interpreter bin")
ok(runner_path.endswith(original_path),
   "existing PATH is preserved after the interpreter bin")
ok(captured.get("env") is not os.environ,
   "runner receives an isolated environment copy")
ok(captured.get("argv", [None])[0] == sys.executable,
   "executed-test helper still launches with the active interpreter")
ok(result.get("status") == "success",
   "runner evidence parsing is unchanged")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
