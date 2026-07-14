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
from switchboard.application.commands import review_verdicts as review_commands  # noqa: E402

P = "switchboard"
AGENT = "codex/SESSION-6-merge-gate"
REVIEWER = "codex/COORD-19-independent-review"
RESOLVER = "operator/COORD-19-review-authority"
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


def review_finding(finding_id="COORD19-REVIEW-1"):
    return {
        "id": finding_id,
        "location": "src/switchboard/storage/repositories/shell.py:900",
        "category": "merge_policy",
        "severity": "high",
        "invariant_violated": "Only independently reviewed code may merge.",
        "repair_requirement": "Record a passing exact-head review or resolve the finding.",
        "class": "escalate",
        "state": "open",
    }


def record_review(created, head_sha, *, status="pass", findings=None):
    return store.record_review_verdict(
        {
            "task_id": created["task_id"],
            "pr_url": f"https://github.com/{REPO}/pull/61",
            "head_sha": head_sha,
            "reviewer_principal": REVIEWER,
            "status": status,
            "findings": [] if findings is None else findings,
        },
        actor=REVIEWER,
        principal_id="principal-coord19-reviewer",
        project=P,
    )


def ready_task(title, head_sha="feedfacefeedfacefeedfacefeedfacefeedface",
               record_passing_review=True):
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
    if record_passing_review:
        review = record_review(created, head_sha)
        ok(review.get("created") is True, f"{title}: passing exact-head review records")
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

    no_review_task, no_review_claim, no_review_branch, no_review_sha = ready_task(
        "missing review verdict blocks", record_passing_review=False)
    no_review = store.merge_gate(
        gate_payload(no_review_task, no_review_claim, no_review_branch, no_review_sha),
        actor="test", project=P)
    no_review_finding = next(
        (item for item in no_review["findings"] if item["code"] == "review_required"),
        None,
    )
    ok(not no_review["ok"] and no_review_finding is not None
       and no_review_sha in no_review_finding["message"],
       "merge_gate self-explains that current-head review is required")

    open_task, open_claim, open_branch, open_sha = ready_task(
        "open review finding blocks", record_passing_review=False)
    changes = record_review(
        open_task, open_sha, status="changes_requested",
        findings=[review_finding()])
    ok(changes.get("created") is True,
       "independent changes-requested verdict records one open finding")
    open_gate = store.merge_gate(
        gate_payload(open_task, open_claim, open_branch, open_sha),
        actor="test", project=P)
    open_gate_finding = next(
        (item for item in open_gate["findings"] if item["code"] == "open_review_findings"),
        None,
    )
    ok(not open_gate["ok"] and open_gate_finding is not None
       and open_gate_finding["message"].startswith("1 open review finding"),
       "merge_gate self-explains the exact open-finding count and head")

    resolution_payload = {
        "task_id": open_task["task_id"],
        "head_sha": open_sha,
        "finding_id": "COORD19-REVIEW-1",
        "state": "waived",
        "resolved_reason": "Authorized product owner accepts this bounded risk.",
        "resolved_sha": open_sha,
        "resolver_principal": RESOLVER,
    }
    forbidden = review_commands.resolve_finding_mapping(
        resolution_payload, actor=RESOLVER,
        principal_id="principal-coord19-resolver", authorized=False, project=P)
    ok(forbidden.get("error_code") == "review_resolution_forbidden",
       "finding waiver fails closed without explicit admin authority")
    waived = review_commands.resolve_finding_mapping(
        resolution_payload, actor=RESOLVER,
        principal_id="principal-coord19-resolver", authorized=True, project=P)
    ok(waived.get("resolved") is True
       and waived["finding"]["state"] == "waived"
       and waived["finding"]["resolved_principal_id"] == "principal-coord19-resolver"
       and waived["verdict"]["status"] == "pass",
       "authorized waiver is durable and promotes the no-open-findings verdict to pass")
    waived_gate = store.merge_gate(
        gate_payload(open_task, open_claim, open_branch, open_sha),
        actor="test", project=P)
    ok(waived_gate["ok"] is True
       and waived_gate["review_gate"]["open_finding_count"] == 0,
       "recorded waiver unblocks the exact-head merge gate")
    with store._conn(P) as c:
        resolution_events = c.execute(
            "SELECT payload FROM activity WHERE task_id=? "
            "AND kind='review.finding_resolved'",
            (open_task["task_id"],),
        ).fetchall()
    ok(len(resolution_events) == 1
       and '"reviewer_quality_signal": "waived"' in resolution_events[0]["payload"],
       "waiver writes one auditable reviewer-quality event")

    # The third unresolved review round adds a deterministic COORD-6 escalation finding.
    round_sha_2 = "2" * 40
    round_sha_3 = "3" * 40
    for index, round_sha in enumerate((round_sha_2, round_sha_3), start=2):
        store.mark_task_pr_opened(
            no_review_task["task_id"], 61, f"https://github.com/{REPO}/pull/61",
            no_review_branch, round_sha, actor="github-webhook", project=P)
        record_review(
            no_review_task, round_sha, status="changes_requested",
            findings=[review_finding(f"COORD19-ROUND-{index}")])
    # The original missing-verdict round had no record; add a historical first round,
    # then restore round three as the current head.
    store.mark_task_pr_opened(
        no_review_task["task_id"], 61, f"https://github.com/{REPO}/pull/61",
        no_review_branch, no_review_sha, actor="github-webhook", project=P)
    record_review(
        no_review_task, no_review_sha, status="changes_requested",
        findings=[review_finding("COORD19-ROUND-1")])
    store.mark_task_pr_opened(
        no_review_task["task_id"], 61, f"https://github.com/{REPO}/pull/61",
        no_review_branch, round_sha_3, actor="github-webhook", project=P)
    rounds_gate = store.merge_gate(
        gate_payload(
            no_review_task, no_review_claim, no_review_branch, round_sha_3,
            github_pr=github_pr(no_review_task["task_id"], no_review_branch, round_sha_3),
            executed_test_run=executed_test_run(
                no_review_task["task_id"], no_review_branch, round_sha_3,
                no_review_claim["work_session_id"]),
        ),
        actor="test", project=P)
    ok(rounds_gate["review_gate"]["round"] == 3
       and rounds_gate["review_gate"]["escalation_task_id"] == "COORD-6"
       and any(item["code"] == "review_round_limit_reached"
               for item in rounds_gate["findings"]),
       "bounded unresolved review rounds deterministically escalate through COORD-6")

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
