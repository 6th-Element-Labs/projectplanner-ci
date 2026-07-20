#!/usr/bin/env python3
"""COORD-20: changes_requested becomes bounded, hands-off remediation work."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile


TMP = tempfile.mkdtemp(prefix="coord20-auto-remediation-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SQLITE_SINGLE_WRITER"] = "1"
os.environ["PM_REVIEW_REMEDIATION_MAX_ROUNDS"] = "2"

import store  # noqa: E402
from switchboard.application.commands import review_verdicts as commands  # noqa: E402
from switchboard.storage.repositories.review_remediations import (  # noqa: E402
    ACCEPTANCE_SCHEMA,
    REMEDIATION_METRICS_SCHEMA,
)
from switchboard.storage.repositories import (  # noqa: E402
    review_remediations as remediation_store,
)


PROJECT = "switchboard"
WORKER = "codex/coord20-worker"
REVIEWER = "codex/coord20-independent-reviewer"
WORKER_PRINCIPAL = "principal-coord20-worker"
REVIEWER_PRINCIPAL = "principal-coord20-reviewer"
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/620"
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def finding(fid, *, category="authorization", finding_class="auto"):
    return {
        "id": fid,
        "location": "src/switchboard/domain/example.py:42",
        "category": category,
        "severity": "high",
        "invariant_violated": "The reviewed invariant must remain fail closed.",
        "repair_requirement": "Implement and test the exact required repair.",
        "class": finding_class,
        "state": "open",
    }


def verdict(task_id, head_sha, findings, *, mode="standard", status="changes_requested",
            pr_url=PR_URL):
    return {
        "task_id": task_id,
        "pr_url": pr_url,
        "head_sha": head_sha,
        "reviewer_principal": REVIEWER,
        "review_mode": mode,
        "status": status,
        "findings": findings,
    }


def reviewable_task(title, head_sha, *, original_exit="Original acceptance"):
    task = store.create_task({
        "workstream_id": "COORD",
        "title": title,
        "exit_criteria": original_exit,
    }, actor="coord20-test", project=PROJECT)
    task_id = task["task_id"]
    store.register_agent(
        WORKER, "codex", lane="COORD", task_id=task_id, project=PROJECT)
    store.register_agent(
        REVIEWER, "codex", lane="COORD", task_id=task_id, project=PROJECT)
    claim = store.claim_task(
        task_id, WORKER, principal_id=WORKER_PRINCIPAL,
        actor="coord20-test", project=PROJECT)
    with store._conn(PROJECT) as c:
        c.execute(
            "UPDATE task_claims SET status='completed', completed_at=1 WHERE id=?",
            (claim["claim_id"],),
        )
        c.execute(
            "UPDATE tasks SET status='In Review' WHERE task_id=?", (task_id,),
        )
    store.mark_task_pr_opened(
        task_id, 620, PR_URL, branch=f"codex/{task_id}-fixture",
        head_sha=head_sha, actor="coord20-test", project=PROJECT)
    return task_id


def move_to_review(task_id, head_sha, pr_number=620):
    store.mark_task_pr_opened(
        task_id, pr_number, PR_URL, branch=f"codex/{task_id}-fixture",
        head_sha=head_sha, actor="coord20-test", project=PROJECT)
    with store._conn(PROJECT) as c:
        c.execute(
            "UPDATE tasks SET status='In Review', assignee=NULL WHERE task_id=?",
            (task_id,),
        )


try:
    store.init_project_registry()
    store.init_db(PROJECT)
    write_through_calls = []
    real_write_through = remediation_store._write_through

    def traced_write_through(project, thunk, timeout_s=None):
        write_through_calls.append(project)
        return real_write_through(project, thunk, timeout_s=timeout_s)

    remediation_store._write_through = traced_write_through

    # The current head was otherwise merge-ready: review is the sole blocker.  This
    # makes the changes_requested verdict a conservative, auditable "save".
    task_id = reviewable_task("automatic remediation fixture", HEAD_1)
    store.append_activity(
        "merge.gate", "coord20-test", {
            "schema": "switchboard.merge_gate.v1",
            "task_id": task_id,
            "head_sha": HEAD_1,
            "ok": False,
            "status": "blocked",
            "findings": [{
                "code": "review_verdict_required",
                "message": "A current-head review verdict is required.",
                "blocking": True,
            }],
        }, task_id=task_id, project=PROJECT)
    concurrency = finding("COORD20-1", category="lease_concurrency")
    first = commands.execute_mapping(
        verdict(task_id, HEAD_1, [concurrency]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    remediation = first.get("auto_remediation") or {}
    detail = store.get_task(task_id, project=PROJECT)
    wakes = [
        row for row in store.list_wake_intents(project=PROJECT)
        if row.get("task_id") == task_id
    ]
    acceptance = json.loads(detail.get("exit_criteria") or "{}")
    ok(first.get("created") is True
       and remediation.get("status") == "queued"
       and remediation.get("needs_lifecycle_ensure") is True,
       "changes_requested durably queues one lifecycle-owned remediation round")
    ok(detail.get("status") == "Not Started" and detail.get("assignee") is None,
       "In Review task is reopened as ready unclaimed remediation work")
    ok(acceptance.get("schema") == ACCEPTANCE_SCHEMA
       and [row["id"] for row in acceptance.get("findings") or []] == ["COORD20-1"],
       "next claim acceptance criteria are exactly the open auto finding")
    ok(len(wakes) == 0,
       "verdict persistence creates no independent worker wake")
    ok(remediation.get("requires_adversarial_review") is True,
       "lease/concurrency remediation forces adversarial re-review on the new head")
    ok(len(write_through_calls) >= 1,
       "queue creation uses the single-writer transaction")

    replay = commands.execute_mapping(
        verdict(task_id, HEAD_1, [concurrency]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    replay_wakes = [
        row for row in store.list_wake_intents(project=PROJECT)
        if row.get("task_id") == task_id
    ]
    ok(replay.get("idempotent_replay") is True
       and replay.get("auto_remediation", {}).get("idempotent_replay") is True
       and len(replay_wakes) == 0,
       "verdict replay creates neither a second remediation nor an independent wake")

    move_to_review(task_id, HEAD_2)
    direct_error = ""
    try:
        store.record_review_verdict(
            verdict(task_id, HEAD_2, [], status="pass"),
            actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    except Exception as exc:
        direct_error = str(getattr(exc, "code", "") or exc)
    ok("adversarial_review_required" in direct_error,
       "persistence rejects store-facade attempts to bypass adversarial re-review")
    standard_pass = commands.execute_mapping(
        verdict(task_id, HEAD_2, [], status="pass"),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    ok(standard_pass.get("error_code") == "adversarial_review_required",
       "a standard review cannot clear a concurrency/lease remediation")
    adversarial_pass = commands.execute_mapping(
        verdict(task_id, HEAD_2, [], status="pass", mode="adversarial"),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    final_detail = store.get_task(task_id, project=PROJECT)
    metrics = final_detail["review_remediation"]["metrics"]
    historical = store.list_review_findings(task_id=task_id, project=PROJECT)
    ok(adversarial_pass.get("created") is True
       and adversarial_pass.get("auto_remediation", {}).get("status") == "resolved",
       "adversarial pass on a new SHA resolves the remediation")
    ok(final_detail.get("exit_criteria") == "Original acceptance"
       and historical[0].get("state") == "fixed"
       and historical[0].get("resolved_sha") == HEAD_2,
       "pass restores original task acceptance and resolves the source-head finding")
    ok(metrics.get("schema") == REMEDIATION_METRICS_SCHEMA
       and metrics.get("work_units_drained") == 1
       and metrics.get("exceptions_resolved_without_human") == 1
       and metrics.get("hands_off_work_unit_rate") == 1.0
       and metrics.get("saves") == 1,
       "hands-off exceptions per drained work unit and saves are queryable proof")
    writes_before_save = len(write_through_calls)
    save_replay = store.record_review_save(
        task_id, HEAD_1, {
            "findings": [{
                "code": "open_review_findings",
                "message": "Review was the sole merge blocker.",
                "blocking": True,
            }],
        }, actor="coord20-test", project=PROJECT)
    ok(save_replay.get("counted") is True
       and save_replay.get("already_counted") is True,
       "merge-gate save accounting is conservative and replay-idempotent")
    ok(len(write_through_calls) == writes_before_save + 1,
       "save state+audit persistence uses one single-writer transaction")

    # Escalate-class findings never become silent automatic acceptance criteria.
    escalation_task = reviewable_task("judgment finding fixture", "3" * 40)
    escalation = commands.execute_mapping(
        verdict(
            escalation_task, "3" * 40,
            [finding("COORD20-E1", finding_class="escalate")],
        ),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    escalation_detail = store.get_task(escalation_task, project=PROJECT)
    escalation_round = escalation.get("auto_remediation") or {}
    ok(escalation_round.get("status") == "escalated"
       and escalation_round.get("human_intervention_required") is True
       and escalation_detail.get("status") == "Blocked",
       "escalate-class finding records a COORD-6 exception and applies the safe block")
    ok(bool(escalation_round.get("decision_id"))
       and isinstance(escalation_round.get("human_escalation"), dict),
       "escalation has an explainable decision id and delivery receipt")
    authority = commands.resolve_finding_mapping(
        {
            "task_id": escalation_task,
            "head_sha": "3" * 40,
            "finding_id": "COORD20-E1",
            "state": "waived",
            "resolved_reason": "COORD-6 authority accepts the bounded safe default.",
            "resolved_sha": "3" * 40,
            "resolver_principal": "operator/coord20-authority",
        },
        actor="operator/coord20-authority",
        principal_id="principal-coord20-authority",
        authorized=True,
        project=PROJECT,
    )
    authority_detail = store.get_task(escalation_task, project=PROJECT)
    authority_metrics = authority_detail["review_remediation"]["metrics"]
    ok(authority.get("verdict", {}).get("status") == "pass"
       and authority.get("auto_remediation", {}).get("status") == "resolved"
       and authority_detail.get("status") == "In Review",
       "authorized COORD-19 resolution closes the escalation and restores review")
    ok(authority_detail.get("exit_criteria") == "Original acceptance"
       and authority_metrics.get("work_units_drained") == 1
       and authority_metrics.get("hands_off_work_units") == 0,
       "human resolution restores original acceptance and is excluded from hands-off proof")

    # Repeated auto rounds are bounded.  New-head review closes the previous unit;
    # round three exceeds the configured budget and stops at COORD-6.
    bounded_task = reviewable_task("bounded remediation fixture", "4" * 40)
    round1 = commands.execute_mapping(
        verdict(bounded_task, "4" * 40, [finding("COORD20-R1")]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    move_to_review(bounded_task, "5" * 40, 621)
    round2 = commands.execute_mapping(
        verdict(bounded_task, "5" * 40, [finding("COORD20-R2")]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    move_to_review(bounded_task, "6" * 40, 622)
    round3 = commands.execute_mapping(
        verdict(bounded_task, "6" * 40, [finding("COORD20-R3")]),
        actor=REVIEWER, principal_id=REVIEWER_PRINCIPAL, project=PROJECT)
    bounded_rows = store.list_review_remediations(
        task_id=bounded_task, project=PROJECT)
    bounded_wakes = [
        row for row in store.list_wake_intents(project=PROJECT)
        if row.get("task_id") == bounded_task
    ]
    ok(round1.get("auto_remediation", {}).get("status") == "queued"
       and round2.get("auto_remediation", {}).get("status") == "queued"
       and round3.get("auto_remediation", {}).get("status") == "escalated",
       "two remediation rounds run automatically and the third fails closed to COORD-6")
    ok(len(bounded_rows) == 3 and len(bounded_wakes) == 0
       and store.get_task(bounded_task, project=PROJECT).get("status") == "Blocked",
       "round budget prevents retry storms without creating worker wakes")

finally:
    if "real_write_through" in locals():
        remediation_store._write_through = real_write_through
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nCOORD-20 auto remediation: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
