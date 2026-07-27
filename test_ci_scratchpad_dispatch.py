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
ok(csd.is_scratchpad_enabled(), "scratchpad enabled by default (CI-12 primary)")
os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "0"
ok(not csd.is_scratchpad_enabled(), "scratchpad can be disabled explicitly")
os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "1"

source_env_names = (
    "SWITCHBOARD_CI_SOURCE_PATH",
    "PM_WORK_SESSION_SOURCE_PATH",
    "PM_REPO_PATH",
)
saved_source_env = {name: os.environ.get(name) for name in source_env_names}
for name in source_env_names:
    os.environ.pop(name, None)
os.environ["PM_REPO_PATH"] = "/var/lib/projectplanner/ci-source"
ok(csd.source_checkout_path() == "/var/lib/projectplanner/ci-source",
   "scratchpad source falls back to the service-owned PM_REPO_PATH clone")
os.environ["SWITCHBOARD_CI_SOURCE_PATH"] = "/srv/switchboard/ci-source"
ok(csd.source_checkout_path() == "/srv/switchboard/ci-source",
   "dedicated scratchpad source overrides PM_REPO_PATH")
for name in source_env_names:
    os.environ.pop(name, None)
try:
    csd.source_checkout_path()
except csd.cvd.CiVerifyDispatchError as exc:
    ok("not configured" in str(exc) and "PM_REPO_PATH" in str(exc),
       "missing source configuration fails closed before Git dispatch")
else:
    ok(False, "missing source configuration fails closed before Git dispatch")
for name, value in saved_source_env.items():
    if value is not None:
        os.environ[name] = value

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
   "dispatch_scratchpad records a disposable ci/pr-* mirror name")
ok(mirror_calls and not mirror_calls[0].get("push_triggered"),
   "scratchpad mirror push cannot execute an agent-authored workflow")
ok(mirror_calls[0].get("mirror_ref_kind") == "tag",
   "projectplanner scratchpad transport uses non-triggering tags")
ok(mirror_calls[0].get("workflow_ref") == "master"
   and mirror_calls[0].get("workflow_inputs", {}).get("purpose") == "head",
   "PR heads dispatch the trusted default-branch full workflow")
ok(mirror_calls[0].get("workflow_inputs", {}).get("source_ref")
   == f"refs/tags/{result['mirror_branch']}",
   "trusted workflow receives the exact disposable scratchpad ref")
ok(mirror_calls[0].get("poll_after_push") is True,
   "scratchpad waits for a terminal external_ci_run instead of leaving triggered evidence")
ok(mirror_calls[0].get("source_fetch_ref") == "refs/pull/412/head",
   "scratchpad fetches the canonical PR ref before resolving the exact SHA locally")
ok(mirror_calls[0].get("cleanup_mirror_branch") is True,
   "scratchpad requests terminal disposable-branch cleanup")

mirror_calls.clear()
ref_result = csd.dispatch_scratchpad_ref(
    VALID_SHA,
    "refs/heads/gh-readonly-queue/master/pr-412-merge",
    label="merge-group",
    project=P,
    source_path=source_path,
    dry_run=False,
)
ok(ref_result["dispatched"] and mirror_calls,
   "merge-group exact-ref dispatch persists the mirror request")
ok(mirror_calls[0].get("poll_after_push") is True,
   "merge-group background dispatch waits for terminal evidence and tag cleanup")
ok(mirror_calls[0].get("source_sha") == VALID_SHA,
   "merge-group dispatch preserves the exact requested SHA")
ok(mirror_calls[0].get("workflow_ref") == "master"
   and mirror_calls[0].get("workflow_inputs", {}).get("purpose") == "merge_group"
   and mirror_calls[0].get("mirror_ref_kind") == "tag",
   "merge groups dispatch the identical verification through the trusted workflow")

try:
    csd.dispatch_scratchpad(
        412,
        head_sha=VALID_SHA,
        purpose="ci_repair",
        project=P,
        source_path=source_path,
        dry_run=False,
    )
except csd.cvd.CiVerifyDispatchError as exc:
    ok("reason" in str(exc).lower(),
       "ci-repair dispatch fails closed without an audit reason")
else:
    ok(False, "ci-repair dispatch fails closed without an audit reason")

mirror_calls.clear()
repair_result = csd.dispatch_scratchpad(
    412,
    head_sha=VALID_SHA,
    purpose="ci_repair",
    actor="operator@example",
    audit_reason="repair the CI control path",
    project=P,
    source_path=source_path,
    dry_run=False,
)
ok(repair_result["dispatched"]
   and mirror_calls[0].get("workflow_inputs", {}).get("purpose") == "ci_repair",
   "ci-repair dispatch runs the identical trusted workflow with audit-only purpose")

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
ci = github_sync._maybe_trigger_ci("6th-Element-Labs/projectplanner", 412, VALID_SHA, P)
ok(ci["scratchpad_dispatched"]
   and ci["pull_model_skip_reason"] == "scratchpad_primary"
   and ci["claim_gate_skip_reason"] == "switchboard_precondition_not_github_status",
   "github_sync routes one CI context and keeps hygiene inside Switchboard")
github_sync.verify_ci_command.verify = orig_verify

os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "0"
ci_pull = github_sync._maybe_trigger_ci("6th-Element-Labs/projectplanner", 412, VALID_SHA, P)
ok(not ci_pull["pull_model_dispatched"]
   and ci_pull["pull_model_skip_reason"] == "pull_model_retired"
   and ci_pull["scratchpad_skip_reason"] == "scratchpad_disabled",
   "disabling scratchpad fails closed instead of reviving the retired pull route")
os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "1"

print(f"\nci_scratchpad_dispatch: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
