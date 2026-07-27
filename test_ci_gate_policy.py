#!/usr/bin/env python3
"""Guard the current Switchboard CI policy.

The single required verdict (`Switchboard CI / VM gate`) runs on
projectplanner-ci from a trusted default-branch workflow. PR heads receive a
fast admission pass; merge-group SHAs receive the full suite and Playwright.
The Plan VM posts only the advisory claim status.
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
least_privilege = Path("deploy/apply-least-privilege.sh").read_text(encoding="utf-8")
mcp_unit = Path("deploy/projectplanner-mcp.service").read_text(encoding="utf-8")

verify = actions_dir / "verify.yml"
_verify = verify.read_text(encoding="utf-8") if verify.exists() else ""
ok(not (actions_dir / "backend-tests.yml").exists()
   and not (actions_dir / "ci-sharded.yml").exists(),
   "retired parallel CI workflows cannot create a second verification path")
ok(verify.exists()
   and "workflow_dispatch:" in _verify
   and '"ci/**"' not in _verify
   and "refs/tags/ci/" in _verify
   and "scripts/switchboard_ci.sh" in _verify,
   "trusted default-branch workflow accepts tags, not mirrored branch pushes")
ok("SWITCHBOARD_APP_ID" in _verify
   and "SWITCHBOARD_APP_PRIVATE_KEY" in _verify
   and "PRIVATE_READ_TOKEN" not in _verify
   and "refs/pull/" not in _verify,
   "scratchpad status callback is App-only and checkout remains public")
ok("Switchboard CI / VM gate" in _verify
   and "Switchboard UI / Playwright" not in _verify
   and "scripts/run_ui_playwright.py" in ci_suite
   and "SWITCHBOARD_CI_STRICT" in _verify
   and "validation_mode" in _verify
   and "SWITCHBOARD_CI_SCOPE" in _verify,
   "one context covers fast head admission and full merge-group verification")
ok("jobs:\n  announce:" in _verify
   and "\n  suite:" in _verify
   and "\n  report:" in _verify,
   "credentials and untrusted test execution live in separate jobs")
ok('DEFAULT_CLAIM_CONTEXT = "Switchboard / claim gate"' in pr_gate,
   "claim gate posts a stable PR-visible commit status context")
ok("run_merge_authorization_for_pr(" not in pr_gate[pr_gate.index("def main("):],
   "claim-gate timer no longer publishes advisory merge-authorization statuses")
ok("import subprocess" not in pr_gate and "import external_ci_mirror" not in pr_gate,
   "switchboard_pr_gate.py has no git/subprocess/external_ci_mirror imports")
ok("projectplanner-claim-gate.timer" in provision and "switchboard_ci.sh" in provision,
   "Provisioning docs install the claim-gate timer and strict local suite")
ok("Switchboard CI / VM gate" in runbook
   and "projectplanner-ci" in runbook,
   "Runbook names VM verification on projectplanner-ci")
ok("ci_scratchpad_dispatch" in Path("github_sync.py").read_text(encoding="utf-8"),
   "github_sync routes canonical PR verification through scratchpad dispatch")
ok("fail-on-red" in pr_gate,
   "Manual gate can fail closed when requested")
ok("Environment=PM_AUTH_MODE=required" in web_unit,
   "Production web unit forces PM_AUTH_MODE=required")
ok("Environment=PM_AUTH_MODE=required" in mcp_unit,
   "Production MCP unit forces PM_AUTH_MODE=required")
ok("PM_AUTH_MODE=required" in provision,
   "Provisioning docs make production auth mode explicit")
ok("SWITCHBOARD_CI_SOURCE_PATH=/var/lib/projectplanner/ci-source" in web_unit
   and "git clone --no-checkout" in least_privilege
   and "credential.helper" in least_privilege,
   "production webhook has a writable authenticated coordination clone for scratchpad mirroring")
ok("claim_gate_prs" in Path("jobs.py").read_text(encoding="utf-8")
   and "Environment=HOME=/var/lib/projectplanner" in
   Path("deploy/projectplanner-claim-gate.service").read_text(encoding="utf-8"),
   "claim-gate job and service are wired for the Plan VM")
ok("run_discovered_tests" in ci_suite and "TEST_DENYLIST" in ci_suite
   and "find ." in ci_suite,
   "CI gate discovers every Python test unless the documented denylist excludes it")
ok(not any(line.startswith("run_test test_") for line in ci_suite.splitlines()),
   "CI gate cannot silently regress to a hand-maintained per-test allowlist")
ok("SWITCHBOARD_CI_SCOPE" in ci_suite
   and "select_impacted_tests.py" in ci_suite
   and "SWITCHBOARD_CI_FAIL_FAST" in ci_suite,
   "fast admission selects impacted tests and stops scheduling after a known failure")
ok("app.js composition root stays below 5,000 lines" not in
   Path("test_arch_ms21_frontend_modules.py").read_text(encoding="utf-8"),
   "retired global app.js line counter is no longer a blocking test")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
