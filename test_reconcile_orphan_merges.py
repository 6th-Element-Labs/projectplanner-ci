#!/usr/bin/env python3
"""Reconcile <-> orphan_merge_discovery integration — merged PRs repair tasks
that bypassed the board workflow, through the real store.reconcile() wiring.

Module-level semantics (parsing, ambiguity, wrong-repo, active claims) are
covered by test_orphan_merge_discovery.py; this proof pins the store side:
token sourcing, checks surfacing, stamping, idempotence, and the no-repo /
no-token boundaries.
"""
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

import orphan_merge_discovery  # noqa: E402
import store  # noqa: E402

HOME = "qa-orphan-home"
REPO = "example/qa-orphan-repo"
SHA_A = "a" * 40
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _merged_pr(number, task_id, merge_sha):
    return {
        "number": number,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "title": f"{task_id}: shipped without claim",
        "merged_at": "2026-07-07T00:00:00Z",
        "merge_commit_sha": merge_sha,
        "base": {"ref": "main", "repo": {"default_branch": "main"}},
        "head": {"ref": f"cursor/{task_id}-work", "sha": "c" * 40},
    }


def _set_token():
    os.environ["PM_GITHUB_TOKEN"] = "test-token"


def _clear_tokens():
    for name in ("PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN"):
        os.environ.pop(name, None)


try:
    store.init_project_registry()
    store.create_project("Orphan Home", project_id=HOME, actor="test")
    store.init_db(HOME)
    store.set_project_github_repo(REPO, project=HOME)
    store.set_project_repo_topology(project=HOME, canonical_repo=REPO)

    orphan = store.create_task({"workstream_id": "ORPH", "title": "merged behind the board"},
                               actor="test", project=HOME)
    cancelled = store.create_task({"workstream_id": "ORPH", "title": "cancelled work"},
                                  actor="test", project=HOME)
    store.update_task(cancelled["task_id"], {"status": "Cancelled"}, actor="test", project=HOME)

    fake_prs = [
        _merged_pr(500, orphan["task_id"], SHA_A),
        _merged_pr(502, "OTHERBOARD-9", "d" * 40),  # sibling-board id: ignored
        _merged_pr(503, cancelled["task_id"], "e" * 40),  # cancelled: skipped
    ]

    def _fake_fetch(repo, token="", lookback_days=30, now=None):
        return (list(fake_prs) if repo == REPO else []), {"merged_pr_count": len(fake_prs)}

    orphan_merge_discovery.fetch_recent_merged_prs = _fake_fetch
    live_pr_calls = []

    def _fake_github_pr(repo, pr_number, token=""):
        live_pr_calls.append((repo, pr_number))
        return next((p for p in fake_prs if p["number"] == pr_number), None)

    store._github_pr = _fake_github_pr

    _set_token()
    result = store.reconcile(project=HOME)
    checks = result.get("external_checks") or {}
    ok(checks.get("orphan_merge_discovery") == "checked",
       "discovery runs through reconcile when repo + token are configured")

    t = store.get_task(orphan["task_id"], project=HOME) or {}
    ok(t.get("status") == "Done", "orphan task stamped Done from merged PR")
    ok((t.get("provenance") or {}).get("terminal") is True,
       "orphan task has terminal merge provenance")
    gs = t.get("git_state") or {}
    ok(gs.get("merged_sha") == SHA_A and gs.get("pr_number") == 500,
       "orphan git_state carries merged_sha + pr_number from discovery")
    swept = [b for b in (result.get("backfilled") or [])
             if b.get("source") == "orphan_merge_discovery"]
    ok([b["task_id"] for b in swept] == [orphan["task_id"]],
       "backfilled reports exactly the orphan repair")
    kinds = [a.get("kind") for a in (t.get("activity") or [])]
    ok("git.orphan_merge_discovered" in kinds,
       "repair leaves a git.orphan_merge_discovered activity trail")

    ok((store.get_task(cancelled["task_id"], project=HOME) or {}).get("status") == "Cancelled",
       "cancelled task untouched")

    again = store.reconcile(project=HOME)
    swept_again = [b for b in (again.get("backfilled") or [])
                   if b.get("source") == "orphan_merge_discovery"]
    ok(not swept_again, "second reconcile run is idempotent (git_state no longer empty)")
    ok(len(live_pr_calls) == 0,
       "terminal GitHub merge provenance is not re-polled on later reconcile runs")
    ok((again.get("external_checks") or {}).get("github_prs_skipped_immutable") == 1,
       "reconcile reports the immutable PR lookup it skipped")

    # If terminal proof becomes incomplete, fail-safe behavior resumes live checking.
    with store._conn(HOME) as c:
        c.execute("UPDATE task_git_state SET in_main_content=0 WHERE task_id=?",
                  (orphan["task_id"],))
    store.reconcile(project=HOME)
    ok(len(live_pr_calls) == 1,
       "incomplete terminal provenance still receives a live GitHub PR check")

    # Without any GitHub token the discovery must fail soft and touch nothing.
    fresh = store.create_task({"workstream_id": "ORPH", "title": "second orphan"},
                              actor="test", project=HOME)
    fake_prs.append(_merged_pr(504, fresh["task_id"], "f" * 40))
    _clear_tokens()
    no_token = store.reconcile(project=HOME)
    ok((no_token.get("external_checks") or {}).get("orphan_merge_discovery") == "skipped_no_token",
       "missing token reports skipped_no_token instead of guessing")
    ok(any(f.get("code") == "orphan_merge_discovery_skipped_no_token"
           for f in no_token.get("findings") or []),
       "missing token surfaces an actionable finding")
    ok((store.get_task(fresh["task_id"], project=HOME) or {}).get("status") == "Not Started",
       "no stamping happens without a token")

    # A project with no canonical repo never attempts discovery.
    AUX = "qa-orphan-aux"
    store.create_project("Orphan Aux", project_id=AUX, actor="test")
    store.init_db(AUX)
    _set_token()
    aux = store.reconcile(project=AUX)
    ok((aux.get("external_checks") or {}).get("orphan_merge_discovery") == "skipped_no_repo",
       "no canonical repo -> discovery skipped_no_repo")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\norphan merge discovery via reconcile: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
