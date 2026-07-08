#!/usr/bin/env python3
"""SESSION-6 safe merge gate regressions."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="merge-gate-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
AGENT = "codex/SESSION-6-merge-gate"
REPO = "6th-Element-Labs/projectplanner"
CI_CONTEXT = "switchboard-ci/full-suite"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def task(title):
    return store.create_task(
        {
            "workstream_id": "SESSION",
            "title": title,
            "exit_criteria": "merge gate required before merge",
        },
        actor="test",
        project=P,
    )


def session_payload(task_id, branch, head_sha):
    return {
        "task_id": task_id,
        "agent_id": AGENT,
        "runtime": "codex",
        "repo_role": "canonical",
        "repo": REPO,
        "default_branch": "master",
        "branch": branch,
        "upstream": f"origin/{branch}",
        "base_sha": "base-ok",
        "head_sha": head_sha,
        "worktree_path": f"/tmp/{task_id.lower()}-merge-gate",
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "policy_profile": "code_strict",
        "hygiene": {
            "repo_preflight": {
                "schema": "switchboard.repo_preflight.v1",
                "ok": True,
                "verdict": "pass",
                "repo_role": "canonical",
                "branch": branch,
                "head_sha": head_sha,
                "base_distance": {"ahead": 1, "behind": 0},
                "findings": [],
            },
        },
    }


def executed_test_run(task_id, branch, head_sha, work_session_id=None, **overrides):
    run = {
        "schema": "switchboard.executed_test_run.v1",
        "run_id": f"run-{task_id.lower()}",
        "work_session_id": work_session_id,
        "branch": branch,
        "head_sha": head_sha,
        "commands": ["python3 test_merge_gate.py"],
        "exit_code": 0,
        "status": "success",
        "completed_at": 1234.0,
        "output_hash": "sha256:" + "b" * 64,
        "runner": "test",
    }
    run.update(overrides)
    return run


def github_pr(task_id, branch, head_sha, repo=REPO, pr_number=61, **overrides):
    pr = {
        "number": pr_number,
        "html_url": f"https://github.com/{repo}/pull/{pr_number}",
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "base": {"ref": "master"},
        "head": {"ref": branch, "sha": head_sha},
        "status_contexts": {CI_CONTEXT: "success"},
        "title": f"{task_id} safe merge",
    }
    pr.update(overrides)
    return pr


def ready_task(title, head_sha="feedfacefeedfacefeedfacefeedfacefeedface"):
    created = task(title)
    branch = f"codex/{created['task_id']}-safe-merge"
    claim = store.claim_task(
        created["task_id"],
        AGENT,
        work_session=session_payload(created["task_id"], branch, head_sha),
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(claim.get("claimed") is True, f"{title}: strict Work Session claim starts")
    completed = store.complete_claim(
        claim["claim_id"],
        evidence={
            "branch": branch,
            "head_sha": head_sha,
            "pr_url": f"https://github.com/{REPO}/pull/61",
            "pr_number": 61,
            "executed_test_run": executed_test_run(
                created["task_id"], branch, head_sha, claim["work_session_id"]),
            "git_diff_check": "clean",
        },
        actor="test",
        project=P,
    )
    ok(completed.get("status") == "In Review", f"{title}: claim completes to In Review")
    store.mark_task_pr_opened(
        created["task_id"], 61, f"https://github.com/{REPO}/pull/61",
        branch, head_sha, actor="github-webhook", project=P)
    return created, claim, branch, head_sha


def gate_payload(created, claim, branch, head_sha, **overrides):
    payload = {
        "task_id": created["task_id"],
        "agent_id": AGENT,
        "claim_id": claim["claim_id"],
        "work_session_id": claim["work_session_id"],
        "repo": REPO,
        "target_branch": "master",
        "branch": branch,
        "head_sha": head_sha,
        "pr_url": f"https://github.com/{REPO}/pull/61",
        "pr_number": 61,
        "require_work_session": True,
        "policy_profile": "code_strict",
        "executed_test_run": executed_test_run(
            created["task_id"], branch, head_sha, claim["work_session_id"]),
        "status_contexts": {CI_CONTEXT: "success"},
        "github_pr": github_pr(created["task_id"], branch, head_sha),
    }
    payload.update(overrides)
    return payload


try:
    store.init_project_registry()
    store.init_db(P)
    store.set_project_repo_topology(
        project=P,
        canonical_repo=REPO,
        canonical_default_branch="master",
        public_ci_repo="6th-Element-Labs/projectplanner-ci",
        public_ci_required_status_contexts=CI_CONTEXT,
    )
    store.register_agent(AGENT, "codex", lane="SESSION", project=P)

    wrong_task, wrong_claim, wrong_branch, wrong_sha = ready_task("wrong repo role blocks")
    wrong = store.merge_gate(
        gate_payload(
            wrong_task,
            wrong_claim,
            wrong_branch,
            wrong_sha,
            repo="6th-Element-Labs/projectplanner-ci",
            pr_url="https://github.com/6th-Element-Labs/projectplanner-ci/pull/61",
            github_pr=github_pr(
                wrong_task["task_id"], wrong_branch, wrong_sha,
                repo="6th-Element-Labs/projectplanner-ci"),
        ),
        actor="test",
        project=P,
    )
    ok(wrong["ok"] is False and
       any(f["code"] == "repo_role_cannot_merge" for f in wrong["findings"]),
       "merge_gate blocks public-CI/evidence-only repo role")

    stale_task, stale_claim, stale_branch, stale_sha = ready_task("stale branch blocks")
    stale = store.merge_gate(
        gate_payload(
            stale_task,
            stale_claim,
            stale_branch,
            stale_sha,
            github_pr=github_pr(stale_task["task_id"], stale_branch, "badc0ffee", behind_by=2),
        ),
        actor="test",
        project=P,
    )
    ok(stale["ok"] is False and
       {f["code"] for f in stale["findings"]} & {"stale_head_sha", "stale_branch"},
       "merge_gate blocks stale branch/head evidence")

    ci_task, ci_claim, ci_branch, ci_sha = ready_task("missing CI blocks")
    missing_ci = store.merge_gate(
        gate_payload(
            ci_task,
            ci_claim,
            ci_branch,
            ci_sha,
            status_contexts={},
            github_pr=github_pr(ci_task["task_id"], ci_branch, ci_sha, status_contexts={}),
        ),
        actor="test",
        project=P,
    )
    ok(missing_ci["ok"] is False and
       any(f["code"] == "missing_required_status_contexts" for f in missing_ci["findings"]),
       "merge_gate blocks missing required CI/status context")

    tests_task, tests_claim, tests_branch, tests_sha = ready_task("missing executed tests blocks")
    missing_tests_payload = gate_payload(tests_task, tests_claim, tests_branch, tests_sha)
    missing_tests_payload.pop("executed_test_run", None)
    missing_tests = store.merge_gate(missing_tests_payload, actor="test", project=P)
    ok(missing_tests["ok"] is False and
       any(f["code"] == "missing_executed_test_run" for f in missing_tests["findings"]),
       "merge_gate blocks missing executed test-run proof")

    ok_task, ok_claim, ok_branch, ok_sha = ready_task("clean merge gate passes")
    passed_gate = store.merge_gate(
        gate_payload(ok_task, ok_claim, ok_branch, ok_sha),
        actor="test",
        project=P,
    )
    ok(passed_gate["ok"] is True and passed_gate["status"] == "passed" and
       passed_gate["done_controlled_by_merge_provenance"] is True,
       "merge_gate passes clean canonical PR without marking Done")
    still_review = store.get_task(ok_task["task_id"], project=P)
    ok(still_review["status"] == "In Review" and
       still_review["provenance"]["type"] == "github_pr_open",
       "passing merge gate leaves task In Review until merge provenance arrives")

    merged = store.mark_task_merged(
        ok_task["task_id"],
        "1234567890abcdef1234567890abcdef12345678",
        61,
        f"https://github.com/{REPO}/pull/61",
        ok_branch,
        ok_sha,
        actor="github-webhook",
        project=P,
    )
    after_merge = store.get_task(ok_task["task_id"], project=P)
    ok(merged["status"] == "Done" and
       after_merge["provenance"]["type"] == "github_pr_merged",
       "merged_sha/default-branch provenance still controls Done")

    # ADR-0006: merge_gate and the SESSION-12 claim gate share one definition of
    # "backed" (store.pr_backed_by_process). A backed task carries the signal; a task
    # with no claim / Work Session / In-Review-or-Done state is blocked as task_not_backed.
    ok(passed_gate.get("backed") is True and passed_gate.get("backing_signal"),
       "merge_gate reports the shared backing signal for a backed task")
    unbacked = task("no board backing at all")
    unbacked_branch = f"codex/{unbacked['task_id']}-x"
    unbacked_gate = store.merge_gate({
        "task_id": unbacked["task_id"], "repo": REPO,
        "pr_url": f"https://github.com/{REPO}/pull/62", "pr_number": 62,
        "branch": unbacked_branch, "head_sha": "a" * 40,
        "github_pr": {"merged": False, "mergeable": True, "draft": False,
                      "base": {"ref": "master", "repo": {"default_branch": "master"}},
                      "head": {"ref": unbacked_branch, "sha": "a" * 40}},
    }, actor="test", project=P)
    ok(unbacked_gate.get("backed") is False and not unbacked_gate.get("ok") and
       any(f["code"] == "task_not_backed" for f in unbacked_gate["findings"]),
       "merge_gate blocks a task with no board backing (shared pr_backed_by_process)")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
