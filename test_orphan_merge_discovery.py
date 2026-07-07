#!/usr/bin/env python3
"""Tests for orphan merged-PR discovery (RECON-11)."""
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="orphan-merge-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")

import orphan_merge_discovery  # noqa: E402
import store  # noqa: E402
import task_id_parser  # noqa: E402

P = "orphan-merge-test"
store.create_project("Orphan Merge Test", project_id=P, actor="test")
store.set_project_github_repo("6th-Element-Labs/projectplanner", project=P)
passed = failed = 0
NOW = 1_700_000_000.0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _merged_pr(number, task_id, *, repo="6th-Element-Labs/projectplanner",
               merged_sha=None, branch=None, title=None):
    merged_sha = merged_sha or ("merge%02d" % number)
    branch = branch or ("codex/%s-slug" % task_id)
    title = title or ("%s: orphan merge test" % task_id)
    return {
        "number": number,
        "html_url": "https://github.com/%s/pull/%d" % (repo, number),
        "title": title,
        "body": "",
        "merged_at": "2026-07-01T12:00:00Z",
        "merge_commit_sha": merged_sha,
        "base": {"ref": "master", "repo": {"default_branch": "master"}},
        "head": {"ref": branch, "sha": ("head%02d" % number)},
        "labels": [],
    }


def _fake_fetch(_repo, *, token="", lookback_days=30, now=None, **_kw):
    return list(_FAKE_MERGED_PRS), {"merged_pr_count": len(_FAKE_MERGED_PRS)}


_FAKE_MERGED_PRS = []


store.init_db(P)

ok(task_id_parser.task_ids_for_pr(_merged_pr(1, "FUSE-1")) == ["FUSE-1"],
   "task_id_parser reads branch/title task ids")

fuse = store.create_task(
    {"workstream_id": "FUSE", "title": "FUSE layer toggles", "sort_order": 10},
    actor="test", project=P,
)
blocked = store.create_task(
    {"workstream_id": "FUSE", "title": "blocked by fuse", "sort_order": 20,
     "depends_on": [fuse["task_id"]]},
    actor="test", project=P,
)
fuse_id = fuse["task_id"]
blocked_id = blocked["task_id"]

_FAKE_MERGED_PRS = [_merged_pr(101, fuse_id)]
store.update_canonical_main_sha("canonicalmain111", actor="test", project=P)
original_fetch = orphan_merge_discovery.fetch_recent_merged_prs
orphan_merge_discovery.fetch_recent_merged_prs = _fake_fetch
os.environ["PM_GITHUB_TOKEN"] = "test-token"
try:
    report = store.reconcile(project=P)
finally:
    orphan_merge_discovery.fetch_recent_merged_prs = original_fetch

repaired = store.get_task(fuse_id, project=P)
blocked_after = store.get_task(blocked_id, project=P)
ok(repaired["status"] == "Done", "FUSE-shaped orphan task moves to Done")
ok(repaired["git_state"]["merged_sha"] == "merge101", "orphan repair stamps merged_sha")
ok(repaired["git_state"]["evidence"].get("source") == "orphan_merge_discovery",
   "orphan repair stamps provenance source")
ok(any(b["task_id"] == fuse_id and b.get("source") == "orphan_merge_discovery"
       for b in report["backfilled"]),
   "orphan backfill is reported")
ok(blocked_after.get("dependency_state", {}).get("ready"),
   "downstream task unblocks after orphan repair")
ok(store.get_meta("canonical_main_sha", project=P) == "canonicalmain111",
   "canonical_main_sha is not rewound by orphan discovery")

with store._conn(P) as c:
    orphan_events = c.execute(
        "SELECT kind, payload FROM activity WHERE task_id=? AND kind=?",
        (fuse_id, "git.orphan_merge_discovered"),
    ).fetchall()
ok(len(orphan_events) == 1, "git.orphan_merge_discovered activity is recorded")

# Idempotent Done
before_sha = store.get_meta("canonical_main_sha", project=P)
report2 = store.reconcile(project=P)
ok(store.get_task(fuse_id, project=P)["status"] == "Done",
   "Done task stays Done on second reconcile")
ok(store.get_meta("canonical_main_sha", project=P) == before_sha,
   "idempotent reconcile preserves canonical_main_sha")

# Ambiguous multiple PRs
ambig = store.create_task(
    {"workstream_id": "QA", "title": "ambiguous orphan"},
    actor="test", project=P,
)
ambig_id = ambig["task_id"]
_FAKE_MERGED_PRS = [
    _merged_pr(201, ambig_id),
    _merged_pr(202, ambig_id, branch="codex/%s-alt" % ambig_id),
]
orphan_merge_discovery.fetch_recent_merged_prs = _fake_fetch
try:
    ambig_report = store.reconcile(project=P)
finally:
    orphan_merge_discovery.fetch_recent_merged_prs = original_fetch
ok(store.get_task(ambig_id, project=P)["status"] != "Done",
   "ambiguous PR matches leave task unchanged")
ok(any(f["task_id"] == ambig_id and f["code"] == "orphan_merge_ambiguous"
       for f in ambig_report["findings"]),
   "ambiguous matches emit orphan_merge_ambiguous")

# Cancelled skipped
cancelled = store.create_task(
    {"workstream_id": "QA", "title": "cancelled orphan"},
    actor="test", project=P,
)
with store._conn(P) as c:
    c.execute("UPDATE tasks SET status='Cancelled' WHERE task_id=?",
              (cancelled["task_id"],))
_FAKE_MERGED_PRS = [_merged_pr(301, cancelled["task_id"])]
orphan_merge_discovery.fetch_recent_merged_prs = _fake_fetch
try:
    store.reconcile(project=P)
finally:
    orphan_merge_discovery.fetch_recent_merged_prs = original_fetch
ok(store.get_task(cancelled["task_id"], project=P)["status"] == "Cancelled",
   "cancelled tasks are skipped")

# Active claim visibility
claimed = store.create_task(
    {"workstream_id": "QA", "title": "claimed orphan"},
    actor="test", project=P,
)
claim = store.claim_task(claimed["task_id"], "agent/orphan", actor="test", project=P)
_FAKE_MERGED_PRS = [_merged_pr(401, claimed["task_id"])]
orphan_merge_discovery.fetch_recent_merged_prs = _fake_fetch
try:
    claim_report = store.reconcile(project=P)
finally:
    orphan_merge_discovery.fetch_recent_merged_prs = original_fetch
ok(any(f["task_id"] == claimed["task_id"] and f["code"] == "orphan_merge_active_claim"
       for f in claim_report["findings"]),
   "active claim conflict is visible in findings")
ok(store.get_task(claimed["task_id"], project=P)["status"] == "Done",
   "orphan repair still stamps Done when claim is active")

# skipped_no_token
no_token_env = {k: os.environ.pop(k, None) for k in (
    "PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN")}
skip_task = store.create_task(
    {"workstream_id": "QA", "title": "no token orphan"},
    actor="test", project=P,
)
_FAKE_MERGED_PRS = [_merged_pr(501, skip_task["task_id"])]
orphan_merge_discovery.fetch_recent_merged_prs = _fake_fetch
try:
    skip_report = store.reconcile(project=P)
finally:
    orphan_merge_discovery.fetch_recent_merged_prs = original_fetch
    for key, value in no_token_env.items():
        if value is not None:
            os.environ[key] = value
ok(any(f.get("code") == "orphan_merge_discovery_skipped_no_token"
       for f in skip_report["findings"]),
   "missing token produces skipped_no_token finding")
ok(skip_report["external_checks"].get("orphan_merge_discovery") == "skipped_no_token",
   "orphan discovery reports skipped_no_token status")

# wrong repo role finding
store.init_db("helm")
store.set_project_github_repo("StevenRidder/Helm", project="helm")
helm_task = store.create_task(
    {"workstream_id": "ENC", "title": "helm orphan"},
    actor="test", project="helm",
)
_FAKE_MERGED_PRS = [{
    **_merged_pr(601, helm_task["task_id"], repo="org/public-ci"),
    "html_url": "https://github.com/org/public-ci/pull/601",
}]
os.environ["PM_GITHUB_TOKEN"] = "test-token"

def role_checker(repo_slug):
    if "public-ci" in repo_slug:
        return {"canonical": False, "role": "public_ci", "matched": True}
    return store.get_project_repo_role(repo_slug, project="helm")

git_states = {helm_task["task_id"]: {}}
tasks = [store.get_task(helm_task["task_id"], project="helm")]
findings, backfilled, checks = orphan_merge_discovery.discover_orphan_merges(
    tasks,
    git_states,
    project="helm",
    repo="StevenRidder/Helm",
    token="test-token",
    fetch_merged_prs_fn=_fake_fetch,
    role_checker=role_checker,
    mark_merged_fn=lambda *a, **k: store.mark_task_merged(*a, **k),
    append_activity_fn=store.append_activity,
)
ok(not backfilled, "wrong-repo-role PR does not stamp canonical Done")
ok(any(f["task_id"] == helm_task["task_id"] and f["code"] == "orphan_merge_wrong_repo_role"
       for f in findings),
   "wrong repo role is reported explicitly")

print(f"\norphan_merge_discovery: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
