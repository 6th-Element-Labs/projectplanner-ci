#!/usr/bin/env python3
"""Native merge-queue wiring: merge_group webhook -> mirror the merge-group head SHA.

Offline unit test (script-style; run directly). Covers github_sync.handle_merge_group,
ci_scratchpad_dispatch.try_dispatch_merge_group's guard rails, and the webhook_inbox route.
The live mirror->verify->status loop can only be exercised with the merge-queue ruleset on;
this pins the branching logic that decides *whether* and *what* to mirror."""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="merge-group-ci-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_scratchpad_dispatch  # noqa: E402
import github_sync  # noqa: E402
import store  # noqa: E402
import webhook_inbox  # noqa: E402

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

mg_payload = {
    "action": "checks_requested",
    "repository": {
        "full_name": "6th-Element-Labs/projectplanner",
        "name": "projectplanner",
        "default_branch": "master",
    },
    "merge_group": {
        "head_sha": VALID_SHA,
        "head_ref": f"refs/heads/gh-readonly-queue/master/pr-999-{VALID_SHA}",
    },
}

# ----- handle_merge_group (via SIMPLIFY-8 verify_ci) ---------------------------------
orig_verify = github_sync.verify_ci_command.verify
captured = {}


def _stub_verify(sha, **k):
    captured["sha"] = sha
    captured["source_fetch_ref"] = k.get("source_fetch_ref")
    captured["ensure"] = k.get("ensure")
    return {
        "ok": True,
        "sha": sha,
        "status": "pending",
        "ensured": True,
        "run_id": "run-mg",
        "ensure_result": {
            "dispatched": True,
            "skip_reason": None,
            "run_id": "run-mg",
            "head_sha": sha,
        },
    }


github_sync.verify_ci_command.verify = _stub_verify

res = github_sync.handle_merge_group(mg_payload, P)
ok(res["action"] == "merge_group_ci_dispatched"
   and res["scratchpad_dispatched"] is True
   and res["scratchpad_run_id"] == "run-mg"
   and res["merge_group_head_sha"] == VALID_SHA,
   "checks_requested on the canonical repo verifies the merge-group head SHA")
ok(captured.get("sha") == VALID_SHA
   and captured.get("ensure") is True
   and str(captured.get("source_fetch_ref") or "").endswith(VALID_SHA),
   "the exact merge-group head SHA + ref are passed through verify_ci")

ignored = github_sync.handle_merge_group({**mg_payload, "action": "destroyed"}, P)
ok(ignored["action"] == "ignored", "a non checks_requested merge_group action is ignored")

nosha = github_sync.handle_merge_group(
    {**mg_payload, "merge_group": {"head_sha": "", "head_ref": ""}}, P)
ok(nosha["action"] == "skipped" and nosha["reason"] == "missing_merge_group_head_sha",
   "a merge_group with no head_sha is skipped, not dispatched")

noncanon = github_sync.handle_merge_group(
    {**mg_payload,
     "repository": {"full_name": "someorg/other", "name": "other", "default_branch": "main"}}, P)
ok(noncanon["action"] == "skipped" and noncanon["reason"] == "repo_role_not_canonical",
   "a merge_group on a non-canonical repo is skipped (verification only, never Done)")

github_sync.verify_ci_command.verify = orig_verify

# ----- try_dispatch_merge_group guard rails (no network) ----------------------------
os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "0"
disabled = ci_scratchpad_dispatch.try_dispatch_merge_group(VALID_SHA, "ref")
ok(disabled["dispatched"] is False and disabled["skip_reason"] == "scratchpad_disabled",
   "try_dispatch_merge_group respects the scratchpad disable flag")

os.environ["SWITCHBOARD_CI_SCRATCHPAD"] = "1"
missing = ci_scratchpad_dispatch.try_dispatch_merge_group("", "ref")
ok(missing["dispatched"] is False and missing["skip_reason"] == "missing_merge_group_head_sha",
   "try_dispatch_merge_group skips when head_sha is empty")

# ----- webhook_inbox routing --------------------------------------------------------
orig_handle = webhook_inbox.github_sync.handle_merge_group
seen = {}


def _spy(payload, project):
    seen["called"] = True
    seen["sha"] = (payload.get("merge_group") or {}).get("head_sha")
    return {"action": "merge_group_ci_dispatched"}


webhook_inbox.github_sync.handle_merge_group = _spy
routed = webhook_inbox._apply_row({"event": "merge_group", "payload": json.dumps(mg_payload)}, P)
ok(seen.get("called") is True and seen.get("sha") == VALID_SHA
   and routed["action"] == "merge_group_ci_dispatched",
   "webhook_inbox routes a merge_group event to handle_merge_group")
webhook_inbox.github_sync.handle_merge_group = orig_handle

print(f"\nmerge_group_ci: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
