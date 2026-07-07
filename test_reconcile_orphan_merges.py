#!/usr/bin/env python3
"""Reconcile orphan-merge sweep — merged PRs stamp tasks that bypassed the board workflow."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="orphan-sweep-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

HOME = "qa-orphan-home"
REPO = "example/qa-orphan-repo"
SHA_A = "a" * 40
SHA_B = "b" * 40
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _fake_pr(number, title, branch, merge_sha, base_ref="main", default_branch="main"):
    return {
        "number": number,
        "title": title,
        "body": "",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "merged_at": "2026-07-07T00:00:00Z",
        "merge_commit_sha": merge_sha,
        "head": {"ref": branch, "sha": "c" * 40},
        "base": {"ref": base_ref, "repo": {"default_branch": default_branch}},
    }


try:
    store.init_project_registry()
    store.create_project("Orphan Home", project_id=HOME, actor="test")
    store.init_db(HOME)
    store.set_project_github_repo(REPO, project=HOME)
    store.set_project_repo_topology(project=HOME, canonical_repo=REPO)

    orphan = store.create_task({"workstream_id": "ORPH", "title": "merged behind the board"},
                               actor="test", project=HOME)
    feature = store.create_task({"workstream_id": "ORPH", "title": "feature-branch merge only"},
                                actor="test", project=HOME)
    cancelled = store.create_task({"workstream_id": "ORPH", "title": "cancelled work"},
                                  actor="test", project=HOME)
    store.update_task(cancelled["task_id"], {"status": "Cancelled"}, actor="test", project=HOME)

    fake_prs = [
        _fake_pr(500, f"{orphan['task_id']}: shipped without claim",
                 f"cursor/{orphan['task_id']}-work", SHA_A),
        # merged into an integration branch, NOT the default branch — must not stamp
        _fake_pr(501, f"{feature['task_id']}: integration merge",
                 f"cursor/{feature['task_id']}-work", SHA_B, base_ref="integration"),
        # references a task id that only exists on a sibling board — must be ignored
        _fake_pr(502, "OTHERBOARD-9: sibling project work", "cursor/OTHERBOARD-9-x", "d" * 40),
        # references a cancelled task — must be skipped
        _fake_pr(503, f"{cancelled['task_id']}: too late", f"cursor/{cancelled['task_id']}-x",
                 "e" * 40),
    ]
    store._github_merged_prs = lambda repo, token="", limit=30: fake_prs if repo == REPO else []
    store._github_pr = lambda repo, pr_number, token="": next(
        (p for p in fake_prs if p["number"] == pr_number), None)

    os.environ["PM_RECONCILE_PR_SWEEP_LIMIT"] = "30"
    result = store.reconcile(project=HOME)
    checks = result.get("external_checks") or {}
    ok(checks.get("github_merged_pr_sweep") == "swept_4", "sweep ran over the merged PR page")

    t = store.get_task(orphan["task_id"], project=HOME) or {}
    ok(t.get("status") == "Done", "orphan task stamped Done from merged PR")
    ok((t.get("provenance") or {}).get("terminal") is True,
       "orphan task has terminal merge provenance")
    gs = t.get("git_state") or {}
    ok(gs.get("merged_sha") == SHA_A and gs.get("pr_number") == 500,
       "orphan git_state carries merged_sha + pr_number from the sweep")
    swept = [b for b in (result.get("backfilled") or [])
             if b.get("source") == "orphan_merge_sweep"]
    ok([b["task_id"] for b in swept] == [orphan["task_id"]],
       "backfilled reports exactly the orphan task from the sweep")
    ok(any(f.get("code") == "orphan_merge_backfilled" for f in result.get("findings") or []),
       "orphan_merge_backfilled finding emitted")

    ok((store.get_task(feature["task_id"], project=HOME) or {}).get("status") == "Not Started",
       "non-default-branch merge does not stamp")
    ok((store.get_task(cancelled["task_id"], project=HOME) or {}).get("status") == "Cancelled",
       "cancelled task untouched")

    again = store.reconcile(project=HOME)
    swept_again = [b for b in (again.get("backfilled") or [])
                   if b.get("source") == "orphan_merge_sweep"]
    ok(not swept_again, "second reconcile run is idempotent (no re-backfill)")

    os.environ["PM_RECONCILE_PR_SWEEP_LIMIT"] = "0"
    disabled = store.reconcile(project=HOME)
    ok((disabled.get("external_checks") or {}).get("github_merged_pr_sweep") == "disabled",
       "PM_RECONCILE_PR_SWEEP_LIMIT=0 disables the sweep")
    os.environ["PM_RECONCILE_PR_SWEEP_LIMIT"] = "30"

    # No canonical repo configured at all — the sweep is never attempted.
    # (get_project_github_repo returns the topology canonical repo by definition,
    # so a configured-but-non-canonical repo cannot reach the sweep; the in-code
    # role guard is defense-in-depth.)
    AUX = "qa-orphan-aux"
    store.create_project("Orphan Aux", project_id=AUX, actor="test")
    store.init_db(AUX)
    aux = store.reconcile(project=AUX)
    ok("github_merged_pr_sweep" not in (aux.get("external_checks") or {}),
       "no canonical repo configured -> sweep not attempted")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\norphan merge sweep: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
