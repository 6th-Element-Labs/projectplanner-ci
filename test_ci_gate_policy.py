#!/usr/bin/env python3
"""Guard the current Switchboard CI policy.

GitHub Actions currently fails before creating jobs for this private repo, so a
checked-in Actions workflow creates false red checks. Until Actions is proven
available again, the VM-backed commit-status gate is the canonical PR signal.
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
runbook = Path("docs/SWITCHBOARD-RUNBOOK.md").read_text(encoding="utf-8")
provision = Path("deploy/PROVISION.md").read_text(encoding="utf-8")
web_unit = Path("deploy/projectplanner.service").read_text(encoding="utf-8")
mcp_unit = Path("deploy/projectplanner-mcp.service").read_text(encoding="utf-8")

ok(not workflow_files,
   "GitHub Actions workflows are absent while Actions startup fails before jobs")
ok("DEFAULT_CONTEXT = \"Switchboard CI / VM gate\"" in pr_gate,
   "VM gate posts a stable PR-visible commit status context")
ok("projectplanner-ci-gate.timer" in provision and "scripts/switchboard_ci.sh" in provision,
   "Provisioning docs install the VM gate timer and strict local suite")
ok("Switchboard CI / VM gate" in runbook
   and "GitHub Actions" in runbook
   and "startup_failure" in runbook,
   "Runbook names the canonical VM gate and the GitHub Actions startup-failure exception")
ok("fail-on-red" in pr_gate,
   "Manual gate can fail closed when requested")
ok("Environment=PM_AUTH_MODE=required" in web_unit,
   "Production web unit forces PM_AUTH_MODE=required")
ok("Environment=PM_AUTH_MODE=required" in mcp_unit,
   "Production MCP unit forces PM_AUTH_MODE=required")
ok("PM_AUTH_MODE=required" in provision,
   "Provisioning docs make production auth mode explicit")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
