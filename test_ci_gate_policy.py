#!/usr/bin/env python3
"""Guard the thin-queue Switchboard CI policy.

PR heads, merge-group heads, and CI repairs use one trusted workflow, one
canonical full script, and one required status. Process hygiene stays inside
Switchboard; timing ratchets are scheduled and non-required.
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
ci_suite = Path("scripts/switchboard_ci.sh").read_text(encoding="utf-8")
perf_suite = Path("scripts/switchboard_perf_ci.sh").read_text(encoding="utf-8")
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
ok("SWITCHBOARD_APP_CLIENT_ID" in _verify
   and "app-id:" not in _verify
   and "SWITCHBOARD_APP_PRIVATE_KEY" in _verify
   and "PRIVATE_READ_TOKEN" not in _verify
   and "refs/pull/" not in _verify,
   "scratchpad status callback is App-only and checkout remains public")
ok("Switchboard CI / VM gate" in _verify
   and "Switchboard UI / Playwright" not in _verify
   and "scripts/run_ui_playwright.py" in ci_suite
   and "SWITCHBOARD_CI_STRICT" in _verify
   and "purpose" in _verify
   and 'SWITCHBOARD_CI_SCOPE: "full"' in _verify,
   "one context and identical full script cover head, queue, and repair SHAs")
ok("jobs:\n  announce:" in _verify
   and "\n  suite:" in _verify
   and "\n  report:" in _verify,
   "credentials and untrusted test execution live in separate jobs")
github_sync = Path("github_sync.py").read_text(encoding="utf-8")
ok("_maybe_refresh_claim_gate" not in github_sync
   and "projectplanner-claim-gate.timer" not in provision,
   "claim and Work Session hygiene stay inside Switchboard, not GitHub statuses")
ok("Switchboard CI / VM gate" in runbook
   and "projectplanner-ci" in runbook,
   "Runbook names VM verification on projectplanner-ci")
ok("ci_scratchpad_dispatch" in github_sync,
   "github_sync routes canonical PR verification through scratchpad dispatch")
repair = Path("scripts/ci_repair_merge.py").read_text(encoding="utf-8")
ok("--admin" in repair
   and "--match-head-commit" in repair
   and "ci-repair" in repair
   and "base branch moved" in repair,
   "repair lane audits and bypasses only the queue after exact-SHA green")
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
ok("run_discovered_tests" in ci_suite and "TEST_DENYLIST" in ci_suite
   and "find ." in ci_suite,
   "CI gate discovers every Python test unless the documented denylist excludes it")
ok(not any(line.startswith("run_test test_") for line in ci_suite.splitlines()),
   "CI gate cannot silently regress to a hand-maintained per-test allowlist")
ok("test_concurrent_load_ratchet.py" in ci_suite
   and "test_cross_process_load_ratchet.py" in ci_suite
   and "concurrent_load_gate.py" not in ci_suite
   and "cross_process_load_gate.py" not in ci_suite
   and "concurrent_load_gate.py" in perf_suite
   and "cross_process_load_gate.py" in perf_suite,
   "timing ratchets are scheduled monitoring, never PR or queue blockers")
perf_workflow = (actions_dir / "performance-monitor.yml").read_text(encoding="utf-8")
ok("schedule:" in perf_workflow
   and "scripts/switchboard_perf_ci.sh" in perf_workflow
   and "Switchboard CI / VM gate" not in perf_workflow,
   "scheduled performance workflow publishes no required status")
ok("app.js composition root stays below 5,000 lines" not in
   Path("test_arch_ms21_frontend_modules.py").read_text(encoding="utf-8"),
   "retired global app.js line counter is no longer a blocking test")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
