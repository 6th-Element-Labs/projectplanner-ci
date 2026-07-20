#!/usr/bin/env python3
"""Tests for ci_scratchpad_dispatch (CI-12)."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ci-scratchpad-dispatch-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_scratchpad_dispatch as csd  # noqa: E402
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

os.environ.pop("SWITCHBOARD_CI_SCRATCHPAD", None)
os.environ.pop("SWITCHBOARD_CI_PULL_MODEL", None)
ok(csd.is_scratchpad_enabled(), "scratchpad enabled by default (CI-12 primary)")
os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "0"
ok(not csd.is_scratchpad_enabled(), "scratchpad can be disabled explicitly")
os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "1"

source_path = os.path.join(_TMP, "checkout")
os.makedirs(source_path)

orig_resolve = csd.cvd.resolve_head_sha
orig_verify = csd.cvd.verify_commit_exists
orig_mirror = csd.external_ci_mirror.request_external_ci_mirror_run
csd.cvd.resolve_head_sha = lambda *a, **k: (VALID_SHA, "test", None)
csd.cvd.verify_commit_exists = lambda *a, **k: None
csd.cvd._token = lambda *a, **k: "tok"
mirror_calls = []

def _fake_mirror(request, source_path, actor="system", project=store.DEFAULT_PROJECT, **kwargs):
    mirror_calls.append(request)
    return {"ok": True, "run_id": "run-test", "status": "triggered"}

csd.external_ci_mirror.request_external_ci_mirror_run = _fake_mirror

result = csd.dispatch_scratchpad(
    412,
    head_sha=VALID_SHA,
    project=P,
    source_path=source_path,
    dry_run=False,
)
ok(result["dispatched"] and result["mirror_branch"].startswith("ci/pr-412/"),
   "dispatch_scratchpad records a disposable ci/pr-* branch run")
ok(mirror_calls and mirror_calls[0].get("push_triggered") is True,
   "scratchpad mirror request is push-triggered (no workflow_dispatch)")
ok(mirror_calls[0].get("poll_after_push") is True,
   "scratchpad waits for a terminal external_ci_run instead of leaving triggered evidence")
ok(mirror_calls[0].get("source_fetch_ref") == "refs/pull/412/head",
   "scratchpad fetches the canonical PR ref before resolving the exact SHA locally")
ok(mirror_calls[0].get("cleanup_mirror_branch") is True,
   "scratchpad requests terminal disposable-branch cleanup")

csd.cvd.resolve_head_sha = orig_resolve
csd.cvd.verify_commit_exists = orig_verify
csd.external_ci_mirror.request_external_ci_mirror_run = orig_mirror

orig_verify = github_sync.verify_ci_command.verify
github_sync.verify_ci_command.verify = lambda sha, **k: {
    "ok": True,
    "sha": sha or VALID_SHA,
    "status": "pending",
    "ensured": True,
    "run_id": "run-test",
    "ensure_result": {
        "dispatched": True,
        "skip_reason": None,
        "head_sha": sha or VALID_SHA,
        "run_id": "run-test",
    },
}
orig_claim = github_sync._maybe_refresh_claim_gate
github_sync._maybe_refresh_claim_gate = lambda *a, **k: {"claim_gate_refreshed": True}
ci = github_sync._maybe_trigger_ci("6th-Element-Labs/projectplanner", 412, VALID_SHA, P)
ok(ci["scratchpad_dispatched"] and ci["pull_model_skip_reason"] == "scratchpad_primary",
   "github_sync routes verification through verify_ci when scratchpad is enabled")
github_sync.verify_ci_command.verify = orig_verify
github_sync._maybe_refresh_claim_gate = orig_claim

os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "0"
os.environ["SWITCHBOARD_CI_PULL_MODEL"] = "1"
orig_pull = github_sync._maybe_dispatch_pull_model_ci
github_sync._maybe_dispatch_pull_model_ci = lambda *a, **k: {
    "dispatched": True,
    "skip_reason": None,
    "head_sha": VALID_SHA,
}
ci_pull = github_sync._maybe_trigger_ci("6th-Element-Labs/projectplanner", 412, VALID_SHA, P)
ok(ci_pull["pull_model_dispatched"] and ci_pull["scratchpad_skip_reason"] == "scratchpad_disabled",
   "pull-model remains available when scratchpad is disabled")
github_sync._maybe_dispatch_pull_model_ci = orig_pull
os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "1"
os.environ.pop("SWITCHBOARD_CI_PULL_MODEL", None)

print(f"\nci_scratchpad_dispatch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
