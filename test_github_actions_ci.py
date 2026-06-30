#!/usr/bin/env python3
"""Guard the GitHub Actions CI workflow against runner-startup regressions."""
from pathlib import Path

WORKFLOW = Path(".github/workflows/switchboard-ci.yml")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


text = WORKFLOW.read_text(encoding="utf-8")

ok("runs-on: ubuntu-latest" in text,
   "Switchboard CI uses a GitHub-hosted runner that can actually start")
ok("self-hosted" not in text,
   "Switchboard CI does not depend on unavailable self-hosted runner labels")
ok("actions/checkout@v4" in text,
   "Switchboard CI checks out the repository before running tests")
ok("actions/setup-python@v5" in text and 'python-version: "3.11"' in text,
   "Switchboard CI pins a supported Python runtime")
ok("actions/setup-node@v4" in text,
   "Switchboard CI installs Node for strict frontend syntax checks")
ok("scripts/switchboard_ci.sh" in text and "SWITCHBOARD_CI_STRICT" in text,
   "Switchboard CI runs the strict local gate")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
