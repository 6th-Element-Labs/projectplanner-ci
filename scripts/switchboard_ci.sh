#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
STRICT="${SWITCHBOARD_CI_STRICT:-0}"
REQUIRE_NODE="${SWITCHBOARD_CI_REQUIRE_NODE:-0}"

section() {
  printf '\n== %s ==\n' "$1"
}

run_test() {
  section "$1"
  "$PYTHON" "$1"
}

section "Python runtime"
"$PYTHON" --version

if [ "$STRICT" = "1" ]; then
  section "Python version gate"
  "$PYTHON" - <<'PY'
import sys

if sys.version_info < (3, 10):
    print("Switchboard strict CI requires Python 3.10+ because runtime dependencies include mcp>=1.9.")
    sys.exit(1)
print("Python version is strict-CI compatible.")
PY

  section "Required Python dependencies"
  "$PYTHON" - <<'PY'
import importlib.util
import sys

required = ["fastapi", "httpx", "mcp", "openpyxl", "uvicorn"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    print("Missing required CI dependency module(s): " + ", ".join(missing))
    sys.exit(1)
print("Required dependency modules importable: " + ", ".join(required))
PY
fi

section "Python compile"
"$PYTHON" -m compileall -q . -x '(^|/)(\.git|\.venv|__pycache__)(/|$)|(^|/)\._'

run_test test_activity_payloads.py
run_test test_audit_export.py
run_test test_adapter_conformance.py
run_test test_board_load_perf.py
run_test test_search_tasks_perf.py

section "Concurrent agent-path SLO gate"
CONCURRENT_LOAD_REPORT="${CONCURRENT_LOAD_REPORT:-${TMPDIR:-/tmp}/switchboard-concurrent-load-report.json}" \
  "$PYTHON" scripts/concurrent_load_gate.py

run_test test_plan_health.py
run_test test_sqlite_pragmas.py
run_test test_dispatch_wake.py
run_test test_agent_bootstrap.py
run_test test_agent_host.py
run_test test_bug_intake.py
run_test test_cleanup_lifecycle.py
run_test test_qa9_fail_early_negative.py
run_test test_codex_adapter.py
run_test test_codex_supervisor.py
run_test test_control_plane_fail_fast.py
run_test test_deliverables_breakdown.py
run_test test_deliverables_dogfood.py
run_test test_deliverables_model.py
run_test test_evidence_claims.py
run_test test_external_ci_mirror_evidence.py
run_test test_external_ci_mirror_model.py
run_test test_external_ci_mirror_runner.py
run_test test_external_artifact_roots.py
run_test test_frontend_project_state.py
run_test test_asset_versioning.py
run_test test_github_webhook.py
run_test test_retire_merged_branch.py
run_test test_ci_gate_policy.py
run_test test_langgraph_adapter.py
run_test test_managed_work_session.py
run_test test_merge_gate.py
run_test test_webhook_retry.py
run_test test_mcp_dependencies.py
run_test test_mcp_threadpool.py
run_test test_mission_narrative.py
run_test test_narrate.py
run_test test_mission_coordinator.py
run_test test_mission_page.py
run_test test_project_creation.py
run_test test_access_private_projects.py
run_test test_publication_evidence.py
run_test test_pre_tool_check.py
run_test test_complete_claim_work_session_gate.py
run_test test_task_id_parser.py
run_test test_orphan_merge_discovery.py
run_test test_reconcile_orphan_merges.py
run_test test_open_pr_backstop.py
run_test test_repo_hierarchy_surface.py
run_test test_repo_hygiene_archive.py
run_test test_repo_preflight.py
run_test test_review_preflight.py
run_test test_review_verifier_runs.py
run_test test_runner_environment.py
run_test test_runner_control_api.py
run_test test_session_health.py
run_test test_work_session_health_panel.py
run_test test_session_policy_profiles.py
run_test test_side_effect_ledger.py
run_test test_signals.py
run_test test_surface_parity.py
run_test test_switchboard_runtime.py
run_test test_tally_project_surface.py
run_test test_ui2_kpi_outcomes.py
run_test test_task_move_archive.py
run_test test_web_write_auth.py
run_test test_auth_rate_limit.py
run_test test_switchboard_pr_gate.py
run_test test_pr_provenance_gate.py
run_test test_unattended_proof.py
run_test test_run_session_auto_session.py
run_test test_work_session_test_run.py
run_test test_work_session_model.py
run_test test_work_session_claim_binding.py
run_test test_backup_restore.py
run_test test_uptime_probe.py
run_test test_ui13_multi_project_intake.py

section "Frontend JavaScript syntax"
if command -v node >/dev/null 2>&1; then
  node --check static/app.js
  node --check static/taikun-ui.js
  node --check static/taikun-theme.js
else
  if [ "$REQUIRE_NODE" = "1" ]; then
    echo "Node.js is required for this gate but was not found." >&2
    exit 1
  fi
  echo "SKIP  Node.js not found; JavaScript syntax check is optional outside strict CI."
fi

section "Switchboard CI gate complete"
