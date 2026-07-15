#!/usr/bin/env python3
"""COORD-31: managed CI drops live worker context before tests execute."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
SANITIZER = ROOT / "scripts" / "ci_runtime_env.sh"
CI_SCRIPT = ROOT / "scripts" / "switchboard_ci.sh"

RUNTIME_ONLY = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN",
    "OPENAI_API_KEY",
    "PM_AGENT_HOST_ALLOW_WORK",
    "PM_AGENT_WORK_MODULE",
    "PM_BASE",
    "PM_CO_ACCOUNT_BINDING_JSON",
    "PM_HOST_LANES",
    "PM_MCP_TOKEN",
    "PM_PROJECT",
    "PM_RUNNER_SESSION_ID",
    "PM_TASK_ID",
    "PM_VERIFY_COMPLETION_PUSH",
    "PM_WAKE_ID",
)
PRESERVED = ("HOME", "PATH", "SWITCHBOARD_CI_STRICT")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


probe = (
    "import json, os; "
    f"print(json.dumps({{'runtime': {{k: os.environ.get(k) for k in {RUNTIME_ONLY!r}}}, "
    f"'preserved': {{k: os.environ.get(k) for k in {PRESERVED!r}}}}}))"
)
environment = os.environ.copy()
for name in RUNTIME_ONLY:
    environment[name] = "poison-live-worker-value"
environment.update({
    "HOME": "/tmp/coord31-home",
    "PATH": environment.get("PATH", ""),
    "SWITCHBOARD_CI_STRICT": "1",
})

completed = subprocess.run(
    [
        "bash",
        "-c",
        '. "$1"; exec "$2" -c "$3"',
        "coord31-env-probe",
        str(SANITIZER),
        sys.executable,
        probe,
    ],
    cwd=ROOT,
    env=environment,
    capture_output=True,
    text=True,
    check=False,
)

ok(completed.returncode == 0, "runtime environment sanitizer is sourceable")
try:
    observed = json.loads(completed.stdout)
except (TypeError, json.JSONDecodeError):
    observed = {"runtime": {}, "preserved": {}}
ok(all(observed["runtime"].get(name) is None for name in RUNTIME_ONLY),
   "worker routing, control, and credential variables are absent in tests")
ok(observed["preserved"] == {
    "HOME": "/tmp/coord31-home",
    "PATH": environment["PATH"],
    "SWITCHBOARD_CI_STRICT": "1",
}, "ordinary process and CI controls are preserved")

ci_source = CI_SCRIPT.read_text(encoding="utf-8")
ok('scripts/ci_runtime_env.sh' in ci_source,
   "the discovered-suite entrypoint sources the sanitizer")
sanitizer_source = SANITIZER.read_text(encoding="utf-8")
ok(all(name in sanitizer_source for name in RUNTIME_ONLY),
   "the sanitizer explicitly documents every regression sentinel")

print(f"\nCOORD-31 CI runtime isolation: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
