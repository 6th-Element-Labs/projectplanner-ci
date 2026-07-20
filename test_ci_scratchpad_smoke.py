#!/usr/bin/env python3
"""CI-13 acceptance smoke: scratchpad webhook wiring end-to-end (offline)."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ci-scratchpad-smoke-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github_sync  # noqa: E402
import store  # noqa: E402

P = "switchboard"
VALID_SHA = "abcdef1234567890abcdef1234567890abcdef12"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


store.init_project_registry()
store.init_db(P)
store.set_project_repo_topology(
    project=P,
    canonical_repo="6th-Element-Labs/projectplanner",
    public_ci_repo="6th-Element-Labs/projectplanner-ci",
)
task = store.create_task({"workstream_id": "CI", "title": "smoke task"}, actor="seed", project=P)
pr_payload = {
    "action": "synchronize",
    "repository": {
        "full_name": "6th-Element-Labs/projectplanner",
        "name": "projectplanner",
        "default_branch": "master",
    },
    "pull_request": {
        "number": 999,
        "title": f"fix({task['task_id']}): scratchpad smoke",
        "body": "CI-13 offline smoke",
        "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/999",
        "head": {"ref": f"codex/{task['task_id']}", "sha": VALID_SHA},
        "base": {"ref": "master"},
    },
}

os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "1"
orig_verify = github_sync.verify_ci_command.verify
orig_claim = github_sync._maybe_refresh_claim_gate
github_sync.verify_ci_command.verify = lambda sha, **k: {
    "ok": True,
    "sha": sha or VALID_SHA,
    "status": "pending",
    "ensured": True,
    "run_id": "run-smoke",
    "ensure_result": {
        "dispatched": True,
        "skip_reason": None,
        "head_sha": sha or VALID_SHA,
        "run_id": "run-smoke",
        "mirror_branch": f"ci/pr-999/{VALID_SHA[:12]}",
    },
}
github_sync._maybe_refresh_claim_gate = lambda *a, **k: {"claim_gate_refreshed": True}

opened = github_sync.handle_pr(pr_payload, P)
ok(opened["action"] == "pr_review_recorded"
   and opened["scratchpad_dispatched"]
   and opened["scratchpad_run_id"] == "run-smoke"
   and opened["pull_model_skip_reason"] == "scratchpad_primary",
   "PR webhook triggers verify_ci ensure path and keeps claim gate refresh")

runs = store.list_external_ci_runs(task_id=task["task_id"], project=P)
ok(isinstance(runs, list), "external_ci_runs list API remains available for board evidence")

github_sync.verify_ci_command.verify = orig_verify
github_sync._maybe_refresh_claim_gate = orig_claim

print(f"\nci_scratchpad_smoke: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
