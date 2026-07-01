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
    pr_payload["action"] = "synchronize"
    pr_payload["pull_request"]["head"]["sha"] = "headupdated"
    synced = github_sync.handle_pr(pr_payload, P)
    synced_task = store.get_task(pr_task["task_id"], project=P)
    ok(synced["in_review_tasks"] == [pr_task["task_id"]] and
       synced_task["git_state"]["head_sha"] == "headupdated",
       "PR synchronize updates review head provenance")

    scope_task = store.create_task({"workstream_id": "DOGFOOD", "title": "scope task"},
                                   actor="seed", project=P)
    follow_on = store.create_task({"workstream_id": "ACCESS", "title": "future task"},
                                  actor="seed", project=P)
    broad_body_payload = {
        "action": "opened",
        "repository": {
            "full_name": "6th-Element-Labs/projectplanner",
            "name": "projectplanner",
            "default_branch": "master",
        },
        "pull_request": {
            "number": 48,
            "title": f"docs({scope_task['task_id']}): scope future work",
            "body": f"Live board scope added: {follow_on['task_id']}.",
            "html_url": "https://github.com/6th-Element-Labs/projectplanner/pull/48",
            "head": {
                "ref": f"codex/{scope_task['task_id']}-scope",
                "sha": "scopehead",
            },
            "base": {"ref": "master"},
        },
    }
    broad_opened = github_sync.handle_pr(broad_body_payload, P)
    ok(broad_opened["in_review_tasks"] == [scope_task["task_id"]],
       "PR opened ignores broad body task mentions")
    ok(store.get_task(follow_on["task_id"], project=P)["status"] == "Not Started",
       "broad body task mention does not move follow-on task")

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

    dynamic_created = store.create_project(
        "Vulkan", actor="seed", github_repo="StevenRidder/OpenCPN")
    ok(dynamic_created.get("created") is True,
       "dynamic project can be configured for an external GitHub repo")
    dynamic_task = store.create_task(
        {"workstream_id": "CONVERT", "title": "dynamic external repo merge"},
        actor="seed", project="vulkan")
    dynamic_payload = {
        "action": "opened",
        "repository": {
            "full_name": "StevenRidder/OpenCPN",
            "name": "OpenCPN",
            "default_branch": "master",
        },
        "pull_request": {
            "number": 38,
            "title": f"{dynamic_task['task_id']}: dynamic repo branch merge",
            "body": "",
            "html_url": "https://github.com/StevenRidder/OpenCPN/pull/38",
            "head": {
                "ref": f"codex/{dynamic_task['task_id']}-slice",
                "sha": "dynamichead",
            },
            "base": {"ref": "vulkan/render-core-poc"},
        },
    }
    routed_project = github_sync.resolve_project(dynamic_payload, "")
    ok(routed_project == "vulkan",
       "external repo webhook resolves to configured dynamic project")
    dynamic_opened = github_sync.handle_pr(dynamic_payload, routed_project)
    ok(dynamic_opened["in_review_tasks"] == [dynamic_task["task_id"]] and
       store.get_task(dynamic_task["task_id"], project="vulkan")["status"] == "In Review",
       "dynamic project PR open records In Review on the dynamic board")
    dynamic_payload["action"] = "closed"
    dynamic_payload["pull_request"]["merged"] = True
    dynamic_payload["pull_request"]["merge_commit_sha"] = "dynamicmerge"
    dynamic_merged = github_sync.handle_pr(dynamic_payload, routed_project)
    dynamic_after = store.get_task(dynamic_task["task_id"], project="vulkan")
    ok(dynamic_merged["auto_closed_tasks"] == [dynamic_task["task_id"]] and
       dynamic_after["status"] == "Done" and
       dynamic_after["git_state"]["merged_sha"] == "dynamicmerge",
       "dynamic project non-default PR merge marks task Done")
    ok(not store.get_meta("canonical_main_sha", project="vulkan"),
       "dynamic non-default PR merge does not advance canonical main SHA")

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

    stale_claim_task = store.create_task(
        {"workstream_id": "HARDEN", "title": "stale claim PR evidence"},
        actor="seed", project=P)
    stale_claim = store.claim_task(
        stale_claim_task["task_id"], "codex/stale-claim", actor="seed", project=P)
    old_head = "651ec7b6e85d6f36037f7ab5c2ae676d67e47a14"
    latest_head = "fd5d4cfdcc747c6ada49c552fc8f0b70d0841c94"
    store.complete_claim(
        stale_claim["claim_id"],
        evidence={
            "branch": f"codex/{stale_claim_task['task_id']}-stale",
            "head_sha": old_head,
            "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/49",
        },
        actor="seed",
        project=P)
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
        if int(pr_number) == 49:
            return {
                "merged_at": "2026-06-29T07:30:04Z",
                "merge_commit_sha": "staleclaimmerge",
                "html_url": f"https://github.com/{repo}/pull/{pr_number}",
                "base": {"ref": "master", "repo": {"default_branch": "master"}},
                "head": {"ref": f"codex/{stale_claim_task['task_id']}-stale",
                         "sha": latest_head},
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
    stale_claim_reconciled = store.get_task(stale_claim_task["task_id"], project=P)
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
    ok(stale_claim_reconciled["status"] == "Done" and
       stale_claim_reconciled["git_state"]["pr_number"] == 49 and
       stale_claim_reconciled["git_state"]["merged_sha"] == "staleclaimmerge" and
       stale_claim_reconciled["git_state"]["head_sha"] == latest_head,
       "reconcile stamps merged PR from pr_url-only stale claim evidence")

    store.init_db("helm")
    store.set_project_github_repo("StevenRidder/Helm", project="helm")
    helm_legacy_done = store.create_task(
        {"workstream_id": "OFFLINE", "title": "legacy Done with PR activity"},
        actor="seed", project="helm")
    with store._conn("helm") as c:
        c.execute("UPDATE tasks SET status='Done', updated_at=? WHERE task_id=?",
                  (0, helm_legacy_done["task_id"]))
        c.execute(
            "INSERT INTO activity(task_id, actor, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (helm_legacy_done["task_id"], "legacy", "comment",
             '{"text":"Merged PR: https://github.com/StevenRidder/Helm/pull/777 branch `codex/OFFLINE-99-legacy` head `1111111`"}',
             0),
        )
    store.update_canonical_main_sha("f" * 40, actor="test", project="helm")
    original_github_pr = store._github_pr
    seen_helm = {}

    def fake_helm_pr(repo, pr_number, token=""):
        seen_helm["repo"] = repo
        seen_helm["pr_number"] = int(pr_number)
        return {
            "merged_at": "2026-06-30T01:23:45Z",
            "merge_commit_sha": "helmmerge",
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
            "base": {"ref": "main", "repo": {"default_branch": "main"}},
            "head": {"ref": "codex/OFFLINE-99-legacy", "sha": "2222222"},
        }

    store._github_pr = fake_helm_pr
    try:
        helm_report = store.reconcile(project="helm")
    finally:
        store._github_pr = original_github_pr
    helm_reconciled = store.get_task(helm_legacy_done["task_id"], project="helm")
    ok(helm_report["external_checks"]["git_reachability"] == "skipped_repo_mismatch",
       "Helm reconcile skips local git reachability from the projectplanner checkout")
    ok(seen_helm == {"repo": "StevenRidder/Helm", "pr_number": 777},
       "Helm reconcile uses the project GitHub repo when hydrating legacy Done PR evidence")
    ok(helm_reconciled["status"] == "Done" and
       helm_reconciled["git_state"]["pr_number"] == 777 and
       helm_reconciled["git_state"]["merged_sha"] == "helmmerge",
       "reconcile stamps merged_sha for a legacy Done Helm task from PR activity")
    ok(any(b["task_id"] == helm_legacy_done["task_id"] and
           b["merged_sha"] == "helmmerge" for b in helm_report["backfilled"]),
       "legacy Done Helm PR backfill is reported")
    ok(not any(f["task_id"] == helm_legacy_done["task_id"] and
               f["code"] == "done_without_merged_sha" for f in helm_report["findings"]),
       "legacy Done Helm PR task is not left as Done without provenance")

    print("\n%d passed, %d failed" % (passed, failed))
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
