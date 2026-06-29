#!/usr/bin/env python3
"""Smoke test for default-branch webhook task provenance handling."""
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="switchboard-github-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")

import github_sync  # noqa: E402
import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def activity_count(task_id, kind):
    with store._conn(P) as c:
        return c.execute(
            "SELECT COUNT(*) FROM activity WHERE task_id=? AND kind=?",
            (task_id, kind),
        ).fetchone()[0]


try:
    store.init_project_registry()
    store.init_db(P)
    ready = store.create_task({"workstream_id": "TEST", "title": "ready direct"}, actor="seed", project=P)
    store.update_task(ready["task_id"], {"status": "In Review"}, actor="seed", project=P)
    not_ready = store.create_task({"workstream_id": "TEST", "title": "not ready direct"},
                                  actor="seed", project=P)
    result = store.backfill_default_branch_commits([
        {"id": "abc123", "message": f"fix({ready['task_id']}): direct default proof"},
        {"id": "def456", "message": f"fix({not_ready['task_id']}): should skip"},
    ], branch="master", actor="github-webhook", project=P)
    ok(ready["task_id"] in result["direct_backfilled_tasks"],
       "push webhook backfills eligible In Review task")
    ok(any(s["task_id"] == not_ready["task_id"] and s["reason"] == "status_not_in_review"
           for s in result["direct_backfill_skipped"]),
       "push webhook reports skipped non-review task")
    ready_after = store.get_task(ready["task_id"], project=P)
    not_ready_after = store.get_task(not_ready["task_id"], project=P)
    ok(ready_after["status"] == "Done" and ready_after["git_state"]["merged_sha"] == "abc123",
       "backfilled task is Done with commit provenance")
    ok(not_ready_after["status"] == "Not Started",
       "non-review task is not promoted by default-branch push")

    pr_task = store.create_task({"workstream_id": "HARDEN", "title": "PR lifecycle"},
                                actor="seed", project=P)
    pr_payload = {
        "action": "opened",
        "repository": {
            "full_name": "6th-Element-Labs/projectplanner",
            "name": "projectplanner",
            "default_branch": "master",
        },
        "pull_request": {
            "number": 42,
            "title": f"fix({pr_task['task_id']}): automate Done",
            "body": "Webhook should close the board loop.",
            "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/42",
            "head": {
                "ref": f"codex/{pr_task['task_id']}-github-done",
                "sha": "headabc",
            },
            "base": {"ref": "master"},
        },
    }
    ok(github_sync.resolve_project(pr_payload, "") == P,
       "projectplanner webhook resolves to Switchboard without query-string project")
    opened = github_sync.handle_pr(pr_payload, P)
    ok(opened["in_review_tasks"] == [pr_task["task_id"]],
       "PR opened webhook records referenced task as In Review")
    opened_task = store.get_task(pr_task["task_id"], project=P)
    ok(opened_task["status"] == "In Review" and
       opened_task["git_state"]["pr_number"] == 42 and
       opened_task["git_state"]["head_sha"] == "headabc",
       "PR opened webhook stores PR/head provenance")
    opened_events = activity_count(pr_task["task_id"], "git.pr_opened")
    replay_opened = github_sync.handle_pr(pr_payload, P)
    ok(replay_opened["in_review_tasks"] == [pr_task["task_id"]] and
       activity_count(pr_task["task_id"], "git.pr_opened") == opened_events,
       "PR opened webhook replay is idempotent")

    pr_payload["action"] = "closed"
    pr_payload["pull_request"]["merged"] = True
    pr_payload["pull_request"]["merge_commit_sha"] = "mergeabc"
    merged = github_sync.handle_pr(pr_payload, P)
    ok(merged["auto_closed_tasks"] == [pr_task["task_id"]] and
       store.get_meta("canonical_main_sha", project=P) == "mergeabc",
       "PR merged webhook updates canonical main SHA and closes the task")
    merged_task = store.get_task(pr_task["task_id"], project=P)
    ok(merged_task["status"] == "Done" and
       merged_task["git_state"]["merged_sha"] == "mergeabc" and
       merged_task["git_state"]["in_main_content"] is True,
       "PR merged webhook marks Done with merged_sha provenance")
    merged_events = activity_count(pr_task["task_id"], "git.pr_merged")
    replay_merged = github_sync.handle_pr(pr_payload, P)
    ok(replay_merged["auto_closed_tasks"] == [pr_task["task_id"]] and
       activity_count(pr_task["task_id"], "git.pr_merged") == merged_events,
       "PR merged webhook replay is idempotent")

    release_task = store.create_task({"workstream_id": "HARDEN", "title": "release target"},
                                     actor="seed", project=P)
    release_payload = {
        "action": "closed",
        "repository": {
            "full_name": "6th-Element-Labs/projectplanner",
            "name": "projectplanner",
            "default_branch": "master",
        },
        "pull_request": {
            "number": 43,
            "title": f"fix({release_task['task_id']}): release branch",
            "body": "",
            "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/43",
            "head": {"ref": f"codex/{release_task['task_id']}", "sha": "releasehead"},
            "base": {"ref": "release"},
            "merged": True,
            "merge_commit_sha": "releasesha",
        },
    }
    github_sync.handle_pr(release_payload, P)
    ok(store.get_meta("canonical_main_sha", project=P) == "mergeabc",
       "non-default-branch PR merge does not advance canonical main SHA")

    missing_sha_task = store.create_task({"workstream_id": "HARDEN", "title": "missing sha"},
                                         actor="seed", project=P)
    missing_sha_payload = {
        "action": "closed",
        "repository": {
            "full_name": "6th-Element-Labs/projectplanner",
            "name": "projectplanner",
            "default_branch": "master",
        },
        "pull_request": {
            "number": 44,
            "title": f"fix({missing_sha_task['task_id']}): missing sha",
            "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/44",
            "head": {"ref": f"codex/{missing_sha_task['task_id']}", "sha": "headmissing"},
            "base": {"ref": "master"},
            "merged": True,
            "merge_commit_sha": "",
        },
    }
    missing = github_sync.handle_pr(missing_sha_payload, P)
    ok(missing["reason"] == "missing merge_commit_sha" and
       store.get_task(missing_sha_task["task_id"], project=P)["status"] == "Not Started",
       "PR merged webhook fails closed when merge_commit_sha is missing")

    done_task = store.create_task({"workstream_id": "DOGFOOD", "title": "done provenance guard"},
                                  actor="seed", project=P)
    store.update_task(done_task["task_id"], {"status": "In Review"}, actor="seed", project=P)
    store.mark_task_default_branch_commit(
        done_task["task_id"], "donecommit", branch="master",
        subject="feat(DOGFOOD-1): seed board", actor="seed", project=P)
    done_before = store.get_task(done_task["task_id"], project=P)["git_state"]
    skipped = store.mark_task_pr_opened(
        done_task["task_id"], 46,
        "https://github.com/6th-Element-Labs/projectplanner/pull/46",
        "codex/HARDEN-7-ci-gates", "wronghead", actor="seed", project=P)
    done_after = store.get_task(done_task["task_id"], project=P)["git_state"]
    ok(skipped.get("skipped") and skipped.get("reason") == "task_already_done",
       "PR opened webhook skips already-Done tasks")
    ok(done_after.get("merged_sha") == done_before.get("merged_sha") and
       not done_after.get("pr_number") and done_after.get("branch") == "master",
       "PR opened webhook cannot overwrite Done task provenance")

    activity_task = store.create_task({"workstream_id": "HARDEN", "title": "activity PR evidence"},
                                      actor="seed", project=P)
    store.update_task(activity_task["task_id"], {"status": "In Review"},
                      actor="seed", project=P)
    store.add_comment(
        activity_task["task_id"], "seed",
        f"Ready in PR #47: https://github.com/6th-Element-Labs/projectplanner/pull/47. "
        f"Branch `codex/{activity_task['task_id']}-activity`, head `activityhead`.",
        project=P)

    reconcile_task = store.create_task({"workstream_id": "HARDEN", "title": "reconcile PR merge"},
                                       actor="seed", project=P)
    store.mark_task_pr_opened(
        reconcile_task["task_id"], 45,
        "https://github.com/6th-Element-Labs/projectplanner/pull/45",
        f"codex/{reconcile_task['task_id']}-reconcile", "headrecon",
        actor="seed", project=P)
    original_github_pr = store._github_pr
    original_env = {k: os.environ.get(k) for k in (
        "PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN")}
    seen = {}
    for key in original_env:
        os.environ.pop(key, None)
    os.environ["SWITCHBOARD_CI_GITHUB_TOKEN"] = "ci-status-token"

    def fake_github_pr(repo, pr_number, token=""):
        seen["token"] = token
        if int(pr_number) == 47:
            return {
                "merged_at": "2026-06-29T05:52:17Z",
                "merge_commit_sha": "activitymerge",
                "html_url": f"https://github.com/{repo}/pull/{pr_number}",
                "base": {"ref": "master", "repo": {"default_branch": "master"}},
                "head": {"ref": f"codex/{activity_task['task_id']}-activity",
                         "sha": "activityhead"},
            }
        return {
            "merged_at": "2026-06-29T05:52:17Z",
            "merge_commit_sha": "reconcilemerge",
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
            "base": {"ref": "master", "repo": {"default_branch": "master"}},
            "head": {"ref": f"codex/{reconcile_task['task_id']}-reconcile", "sha": "headrecon"},
        }

    store._github_pr = fake_github_pr
    try:
        report = store.reconcile(project=P)
    finally:
        store._github_pr = original_github_pr
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    reconciled = store.get_task(reconcile_task["task_id"], project=P)
    activity_reconciled = store.get_task(activity_task["task_id"], project=P)
    ok(report["external_checks"]["github_repo"] == "6th-Element-Labs/projectplanner",
       "reconcile uses project-scoped GitHub repo config")
    ok(seen.get("token") == "ci-status-token",
       "reconcile accepts SWITCHBOARD_CI_GITHUB_TOKEN for PR checks")
    ok(activity_reconciled["status"] == "Done" and
       activity_reconciled["git_state"]["pr_number"] == 47 and
       activity_reconciled["git_state"]["merged_sha"] == "activitymerge",
       "reconcile hydrates PR evidence from task activity before merge backfill")
    ok(any(b["task_id"] == reconcile_task["task_id"] for b in report["backfilled"]),
       "reconcile reports PR-merge backfill")
    ok(reconciled["status"] == "Done" and
       reconciled["git_state"]["merged_sha"] == "reconcilemerge",
       "reconcile stamps merged PR as Done with merged_sha provenance")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
