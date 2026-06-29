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

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
