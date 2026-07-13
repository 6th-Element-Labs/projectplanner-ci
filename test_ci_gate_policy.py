#!/usr/bin/env python3
"""Guard the current Switchboard CI policy.

VM verification (`Switchboard CI / VM gate`) runs on projectplanner-ci via the
pull-model verify workflow. The Plan VM posts only the SESSION-12 claim gate.
"""
from pathlib import Path

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


actions_dir = Path(".github/workflows")
workflow_files = sorted(actions_dir.glob("*.yml")) + sorted(actions_dir.glob("*.yaml")) \
    if actions_dir.exists() else []
pr_gate = Path("scripts/switchboard_pr_gate.py").read_text(encoding="utf-8")
ci_suite = Path("scripts/switchboard_ci.sh").read_text(encoding="utf-8")
runbook = Path("docs/SWITCHBOARD-RUNBOOK.md").read_text(encoding="utf-8")
provision = Path("deploy/PROVISION.md").read_text(encoding="utf-8")
web_unit = Path("deploy/projectplanner.service").read_text(encoding="utf-8")
mcp_unit = Path("deploy/projectplanner-mcp.service").read_text(encoding="utf-8")

backend_tests = actions_dir / "backend-tests.yml"
_bt = backend_tests.read_text(encoding="utf-8") if backend_tests.exists() else ""
ok(backend_tests.exists()
   and "workflow_dispatch" in _bt
   and "scripts/switchboard_ci.sh" in _bt,
   "backend-tests workflow runs the full suite on the public projectplanner-ci sandbox")
ok('DEFAULT_CLAIM_CONTEXT = "Switchboard / claim gate"' in pr_gate,
   "claim gate posts a stable PR-visible commit status context")
ok("import subprocess" not in pr_gate and "import external_ci_mirror" not in pr_gate,
   "switchboard_pr_gate.py is claim-only (no git/subprocess/external_ci_mirror imports)")
ok("projectplanner-claim-gate.timer" in provision and "switchboard_ci.sh" in provision,
   "Provisioning docs install the claim-gate timer and strict local suite")
ok("Switchboard CI / VM gate" in runbook
   and "projectplanner-ci" in runbook,
   "Runbook names pull-model VM verification on projectplanner-ci")
ok("fail-on-red" in pr_gate,
   "Manual gate can fail closed when requested")
ok("Environment=PM_AUTH_MODE=required" in web_unit,
   "Production web unit forces PM_AUTH_MODE=required")
ok("Environment=PM_AUTH_MODE=required" in mcp_unit,
   "Production MCP unit forces PM_AUTH_MODE=required")
ok("PM_AUTH_MODE=required" in provision,
   "Provisioning docs make production auth mode explicit")
ok("claim_gate_prs" in Path("jobs.py").read_text(encoding="utf-8")
   and "Environment=HOME=/var/lib/projectplanner" in
   Path("deploy/projectplanner-claim-gate.service").read_text(encoding="utf-8"),
   "claim-gate job and service are wired for the Plan VM")
ok("run_discovered_tests" in ci_suite and "TEST_DENYLIST" in ci_suite
   and "find ." in ci_suite,
   "CI gate discovers every Python test unless the documented denylist excludes it")
ok(not any(line.startswith("run_test test_") for line in ci_suite.splitlines()),
   "CI gate cannot silently regress to a hand-maintained per-test allowlist")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
