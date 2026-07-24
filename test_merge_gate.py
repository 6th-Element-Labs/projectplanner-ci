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


def task(title, description=""):
    return store.create_task(
        {
            "workstream_id": "SESSION",
            "title": title,
            "description": description,
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
    return review_commands.execute_mapping(
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
               record_passing_review=True, description=""):
    created = task(title, description=description)
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

    warning_task, warning_claim, warning_branch, warning_sha = ready_task(
        "non-blocking preflight warning passes")
    warning_session = store.get_work_session(warning_claim["work_session_id"], project=P)
    warning_hygiene = dict(warning_session.get("hygiene") or {})
    warning_hygiene["repo_preflight"] = {
        "schema": "switchboard.repo_preflight.v1",
        "ok": False,
        "verdict": "warn",
        "repo_role": "canonical",
        "branch": warning_branch,
        "head_sha": warning_sha,
        "base_distance": {"ahead": 1, "behind": 0},
        "findings": [{
            "code": "missing_upstream",
            "failure_class": "missing_upstream",
            "severity": "medium",
            "blocking": False,
        }],
    }
    store.update_work_session(
        warning_claim["work_session_id"], {"hygiene": warning_hygiene},
        actor="test", project=P)
    warning_gate = store.merge_gate(
        gate_payload(
            warning_task, warning_claim, warning_branch, warning_sha),
        actor="test", project=P,
    )
    ok(warning_gate["ok"] is True and not any(
        f["code"] == "work_session_preflight_failed"
        for f in warning_gate["findings"]),
       "merge_gate accepts repo preflight with only non-blocking warnings")

    # BUG-177: a workspace off the coordinator's filesystem can never be statted, so
    # preflight_work_session records a `coordinator_unverifiable` (BUG-159) or
    # `agent_host_pending` (BUG-115) report instead — verdict "warn", ok False, one
    # non-blocking finding. That is the ONLY preflight a host-local fleet agent can
    # produce, so the merge gate must accept it; treating it as a failure would make
    # every host-local code_strict PR unmergeable. Pinned because the absence of this
    # coverage led to it being misdiagnosed as unsatisfiable.
    for label, report_extra in (
        ("unverifiable", {"source": "coordinator_unverifiable", "unverifiable": True,
                          "code": "work_session_preflight_unverifiable"}),
        ("pending", {"source": "agent_host_pending", "pending": True,
                     "code": "host_preflight_pending"}),
    ):
        hl_task, hl_claim, hl_branch, hl_sha = ready_task(
            f"host-local {label} preflight passes")
        hl_session = store.get_work_session(hl_claim["work_session_id"], project=P)
        hl_hygiene = dict(hl_session.get("hygiene") or {})
        hl_hygiene["repo_preflight"] = {
            "schema": "switchboard.repo_preflight.v1",
            "ok": False,
            "verdict": "warn",
            "source": report_extra["source"],
            report_extra["source"].split("_")[-1]: True,
            "repo_role": "canonical",
            "branch": hl_branch,
            "head_sha": hl_sha,
            "findings": [{
                "code": report_extra["code"],
                "failure_class": "missing_data",
                "severity": "medium",
                "blocking": False,
            }],
        }
        store.update_work_session(
            hl_claim["work_session_id"], {"hygiene": hl_hygiene},
            actor="test", project=P)
        hl_gate = store.merge_gate(
            gate_payload(hl_task, hl_claim, hl_branch, hl_sha),
            actor="test", project=P,
        )
        ok(hl_gate["ok"] is True and not any(
            f["code"] in {"work_session_preflight_failed",
                          "missing_work_session_preflight"}
            for f in hl_gate["findings"]),
           f"merge_gate accepts a host-local {label} preflight report")

    # ...but a session that never ran preflight at all still blocks, and now says how to fix it.
    none_task, none_claim, none_branch, none_sha = ready_task("no preflight blocks")
    none_session = store.get_work_session(none_claim["work_session_id"], project=P)
    none_hygiene = {k: v for k, v in (none_session.get("hygiene") or {}).items()
                    if k != "repo_preflight"}
    store.update_work_session(
        none_claim["work_session_id"], {"hygiene": none_hygiene},
        actor="test", project=P)
    none_gate = store.merge_gate(
        gate_payload(none_task, none_claim, none_branch, none_sha),
        actor="test", project=P,
    )
    none_finding = next(
        (f for f in none_gate["findings"]
         if f["code"] == "missing_work_session_preflight"), None)
    # _merge_gate_finding splats details into the finding itself, so repair is top-level.
    ok(none_finding is not None
       and none_finding.get("blocking") is True
       and "preflight_work_session" in str(none_finding.get("repair") or ""),
       "a never-run preflight still blocks and names preflight_work_session as the repair")

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
    open_metrics = store.review_remediation_metrics(
        task_id=open_task["task_id"], project=P)
    ok(open_gate.get("review_remediation_save", {}).get("counted") is True
       and open_gate["review_remediation_save"].get("already_counted") is False
       and open_metrics.get("saves") == 1,
       "production merge_gate counts a review-only block as one idempotent save")
    open_gate_replay = store.merge_gate(
        gate_payload(open_task, open_claim, open_branch, open_sha),
        actor="test", project=P)
    replay_metrics = store.review_remediation_metrics(
        task_id=open_task["task_id"], project=P)
    ok(open_gate_replay.get("review_remediation_save", {}).get("already_counted") is True
       and replay_metrics.get("saves") == 1,
       "replayed merge_gate evaluation does not double-count the save")

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

    # COORD-19 liveness: a change that NOBODY reviews must escalate, not block forever.
    # Round-based escalation can never rescue this (the round counter stays 0), so the stall
    # fence is the only way out when no verdict is recorded.
    from switchboard.storage.repositories.review_verdicts import (  # noqa: E402
        REVIEW_STALL_ESCALATION_S,
        review_merge_gate as probe_gate,
        review_merge_gate_findings as probe_findings,
    )
    stall_task, _stall_claim, _stall_branch, stall_sha = ready_task(
        "unreviewed head escalates instead of deadlocking", record_passing_review=False)
    with store._conn(P) as c:
        stall_pushed_at = c.execute(
            "SELECT pushed_at FROM task_git_state WHERE task_id=?",
            (stall_task["task_id"],),
        ).fetchone()["pushed_at"]

    fresh_gate = probe_gate(
        stall_task["task_id"], stall_sha, project=P, now=stall_pushed_at + 60)
    ok(fresh_gate["ok"] is False
       and fresh_gate["code"] == "review_required"
       and fresh_gate["review_stalled"] is False
       and fresh_gate["escalation_required"] is False,
       "a freshly pushed unreviewed head blocks review but does not escalate yet")

    stalled_at = stall_pushed_at + REVIEW_STALL_ESCALATION_S + 60
    stalled_gate = probe_gate(
        stall_task["task_id"], stall_sha, project=P, now=stalled_at)
    ok(stalled_gate["escalation_required"] is True
       and stalled_gate["escalation_reason"] == "review_stalled_no_verdict"
       and stalled_gate["review_stalled"] is True
       and stalled_gate["round"] == 0
       and stalled_gate["escalation_task_id"] == "COORD-6",
       "an unreviewed head past the stall fence escalates COORD-6 at round 0 (no deadlock)")

    _stalled, stalled_findings = probe_findings(
        stall_task["task_id"], stall_sha, project=P, now=stalled_at)
    stall_finding = next(
        (item for item in stalled_findings if item["code"] == "review_stalled_no_verdict"),
        None,
    )
    ok(stall_finding is not None
       and stall_finding["escalation_task_id"] == "COORD-6"
       and "stall, not a queue" in stall_finding["message"],
       "the stall escalation self-explains the silence instead of citing 0 rounds")

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

    # The branch-protection poster (`Switchboard / merge authorization`) calls merge_gate
    # with only the PR facts — no claim_id and no work_session_id. merge_gate used to
    # resolve a session ONLY from an explicit work_session_id or the task's *active*
    # claim, so a code_strict task whose claim had already completed looked sessionless
    # forever: work_session_required + missing_executed_test_run, permanently, no matter
    # how healthy the Work Session recorded against the task was.
    orphan, orphan_claim, orphan_branch, orphan_sha = ready_task(
        "claimless task still resolves its Work Session",
        description="policy_profile:code_strict")
    orphan_session = store.get_work_session(
        orphan_claim["work_session_id"], project=P)
    orphan_hygiene = dict(orphan_session.get("hygiene") or {})
    orphan_hygiene["executed_test_run"] = executed_test_run(
        orphan["task_id"], orphan_branch, orphan_sha,
        orphan_claim["work_session_id"])
    store.update_work_session(
        orphan_claim["work_session_id"], {"hygiene": orphan_hygiene},
        actor="test", project=P)
    ok(not store.get_task(orphan["task_id"], project=P).get("active_claims"),
       "claimless task: the completed claim is no longer active")
    poster_gate = store.merge_gate({
        "task_id": orphan["task_id"], "repo": REPO,
        "target_branch": "master",
        "branch": orphan_branch, "head_sha": orphan_sha,
        "pr_url": f"https://github.com/{REPO}/pull/61", "pr_number": 61,
        "status_contexts": {CI_CONTEXT: "success"},
        "github_pr": github_pr(orphan["task_id"], orphan_branch, orphan_sha),
    }, actor="test", project=P)
    poster_codes = {f["code"] for f in poster_gate.get("findings") or []}
    ok(poster_gate.get("work_session_id") == orphan_claim["work_session_id"],
       "claimless task: merge_gate resolves the Work Session bound to the task")
    ok("work_session_required" not in poster_codes,
       "claimless task: no spurious work_session_required for the poster payload")
    ok("missing_executed_test_run" not in poster_codes,
       "claimless task: the session's executed test run satisfies the tests gate")

    # Fail closed: the task-scoped fallback must never authorize a head the Work Session
    # was not recorded against, or an expired claim would become a way to merge anything.
    other_head = "b" * 40
    other_head_gate = store.merge_gate({
        "task_id": orphan["task_id"], "repo": REPO,
        "target_branch": "master",
        "branch": orphan_branch, "head_sha": other_head,
        "pr_url": f"https://github.com/{REPO}/pull/61", "pr_number": 61,
        "status_contexts": {CI_CONTEXT: "success"},
        "github_pr": github_pr(orphan["task_id"], orphan_branch, other_head),
    }, actor="test", project=P)
    ok(any(f["code"] == "work_session_required"
           for f in other_head_gate.get("findings") or []),
       "claimless task: a session for a different head does not authorize this head")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
