#!/usr/bin/env python3
"""BUG-172: generated and integration proof for cross-task completion authority."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import time


TMP = tempfile.mkdtemp(prefix="bug172-completion-proof-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SQLITE_SINGLE_WRITER"] = "1"

import store  # noqa: E402
from switchboard.application.commands import review_verdicts, submit_bug  # noqa: E402
from switchboard.domain.completion.repair_proof import (  # noqa: E402
    classify_cross_task_repair_proof,
)


PROJECT = "switchboard"
SOURCE_HEAD = "1" * 40
REPAIR_HEAD = "2" * 40
MERGED_SHA = "3" * 40
REVIEWER = "codex/bug172-reviewer"
PRINCIPAL = "principal-bug172-reviewer"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def acceptance(finding_id, *, adversarial=False):
    invariant = "Only exact proof closes this finding."
    if adversarial:
        invariant += " The atomic race and lease fence require adversarial review."
    return {
        "id": finding_id,
        "location": "src/switchboard/domain/completion/example.py:42",
        "category": "atomicity" if adversarial else "routing",
        "severity": "high",
        "invariant_violated": invariant,
        "repair_requirement": "Repair and verify the exact invariant.",
        "class": "auto",
        "state": "open",
    }


def valid_projection():
    findings = [
        {
            "verdict_id": "source-verdict",
            "task_id": "COORD-46",
            "finding_id": finding_id,
            "location": "x.py:1",
            "category": "atomicity",
            "severity": "high",
            "invariant_violated": "atomic race",
            "repair_requirement": "repair",
            "finding_class": "auto",
            "state": "open",
            "resolved_sha": None,
        }
        for finding_id in ("F1", "F2")
    ]
    criteria = [
        {
            "id": row["finding_id"],
            "location": row["location"],
            "category": row["category"],
            "severity": row["severity"],
            "invariant_violated": row["invariant_violated"],
            "repair_requirement": row["repair_requirement"],
            "class": "auto",
        }
        for row in findings
    ]
    return {
        "link": {
            "schema": "switchboard.cross_task_review_repair.v1",
            "status": "linked",
            "repair_task_id": "BUG-172",
            "source_task_id": "COORD-46",
            "source_verdict_id": "source-verdict",
            "remediation_id": "remediation-1",
            "finding_ids": ["F1", "F2"],
        },
        "bug_report": {"source_task": "COORD-46"},
        "remediation": {
            "remediation_id": "remediation-1",
            "task_id": "COORD-46",
            "verdict_id": "source-verdict",
            "source_head_sha": SOURCE_HEAD,
            "source_pr_url": "https://example.test/pull/810",
            "status": "remediating",
            "acceptance_criteria": criteria,
            "auto_finding_count": 2,
            "escalate_finding_count": 0,
            "requires_adversarial_review": True,
            "human_intervention_required": False,
        },
        "source_verdict": {
            "verdict_id": "source-verdict",
            "task_id": "COORD-46",
            "head_sha": SOURCE_HEAD,
            "pr_url": "https://example.test/pull/810",
        },
        "source_findings": findings,
        "repair_task": {"task_id": "BUG-172", "status": "Done"},
        "repair_git": {
            "head_sha": REPAIR_HEAD,
            "pr_url": "https://example.test/pull/900",
            "pr_number": 900,
            "merged_sha": MERGED_SHA,
            "merged_at": 1.0,
            "in_main_content": 1,
        },
        "repair_verdict": {
            "verdict_id": "repair-verdict",
            "task_id": "BUG-172",
            "head_sha": REPAIR_HEAD,
            "pr_url": "https://example.test/pull/900",
            "reviewer_principal_id": PRINCIPAL,
            "review_mode": "adversarial",
            "status": "pass",
            "open_finding_count": 0,
        },
        "merge_gate": {
            "schema": "switchboard.merge_gate.v1",
            "task_id": "BUG-172",
            "head_sha": REPAIR_HEAD,
            "pr_url": "https://example.test/pull/900",
            "pr_number": 900,
            "ok": True,
            "status": "passed",
            "findings": [],
        },
    }


def classify(projection):
    return classify_cross_task_repair_proof(**projection)


def generated_state_space():
    mutators = [
        lambda p: p["link"].update(schema="wrong-schema"),
        lambda p: p["link"].update(status="cancelled"),
        lambda p: p["link"].update(finding_ids=["F1"]),
        lambda p: p["bug_report"].update(source_task="OTHER-1"),
        lambda p: p["remediation"].update(
            acceptance_criteria=p["remediation"]["acceptance_criteria"][:1]),
        lambda p: p["remediation"].update(human_intervention_required=True),
        lambda p: p["source_verdict"].update(
            pr_url="https://example.test/pull/other"),
        lambda p: p["source_findings"][1].update(finding_class="escalate"),
        lambda p: p["repair_task"].update(status="In Review"),
        lambda p: p["repair_git"].update(in_main_content=0),
        lambda p: p["repair_verdict"].update(
            pr_url="https://example.test/pull/other"),
        lambda p: p["repair_verdict"].update(review_mode="standard"),
        lambda p: p["merge_gate"].update(ok=False),
        lambda p: p["merge_gate"].update(
            pr_url="https://example.test/pull/other"),
    ]
    checked = 0
    first_failure = None
    for mask in range(1 << len(mutators)):
        projection = valid_projection()
        for index, mutate in enumerate(mutators):
            if mask & (1 << index):
                mutate(projection)
        result = classify(projection)
        expected_ready = mask == 0
        if (result.get("status") == "ready") != expected_ready:
            first_failure = {"mask": mask, "result": result}
            break
        checked += 1

    replay_base = valid_projection()
    replay_base["link"].update({
        "status": "resolved",
        "repair_head_sha": REPAIR_HEAD,
        "repair_pr_url": "https://example.test/pull/900",
        "repair_pr_number": 900,
        "repair_merged_sha": MERGED_SHA,
        "repair_verdict_id": "repair-verdict",
    })
    replay_base["remediation"].update(
        status="resolved", resolved_head_sha=REPAIR_HEAD)
    for row in replay_base["source_findings"]:
        row.update(state="fixed", resolved_sha=REPAIR_HEAD)
    replay_mutators = [
        lambda p: p["repair_verdict"].update(status="changes_requested"),
        lambda p: p["repair_verdict"].update(head_sha="9" * 40),
        lambda p: p["repair_verdict"].update(pr_url="other"),
        lambda p: p["repair_verdict"].update(review_mode="standard"),
        lambda p: p["repair_verdict"].update(open_finding_count=9),
        lambda p: p["merge_gate"].update(ok=False),
        lambda p: p["merge_gate"].update(status="blocked"),
        lambda p: p["merge_gate"].update(head_sha="9" * 40),
        lambda p: p["merge_gate"].update(pr_url="other"),
        lambda p: p["merge_gate"].update(findings=[{"blocking": True}]),
        lambda p: p["repair_git"].update(pr_url="other", pr_number=999),
        lambda p: p["repair_git"].update(merged_sha="9" * 40),
    ]
    replay_checked = 0
    replay_failure = None
    for mask in range(1 << len(replay_mutators)):
        projection = deepcopy(replay_base)
        for index, mutate in enumerate(replay_mutators):
            if mask & (1 << index):
                mutate(projection)
        result = classify(projection)
        if result.get("status") != "resolved":
            replay_failure = {"mask": mask, "result": result}
            break
        replay_checked += 1
    return checked, first_failure, replay_checked, replay_failure


def create_source(*, adversarial=False, mixed=False):
    task = store.create_task({
        "workstream_id": "COORD",
        "title": "BUG-172 source",
        "exit_criteria": "Original acceptance",
    }, actor="bug172-test", project=PROJECT)
    task_id = task["task_id"]
    pr_url = f"https://example.test/pull/{task_id}"
    store.mark_task_pr_opened(
        task_id, 810, pr_url, branch=f"codex/{task_id}",
        head_sha=SOURCE_HEAD, actor="bug172-test", project=PROJECT)
    findings = [
        acceptance("FIX-1", adversarial=adversarial),
        acceptance("FIX-2", adversarial=adversarial),
    ]
    if mixed:
        findings.append({
            **acceptance("HUMAN-1"),
            "class": "escalate",
            "category": "judgment",
        })
    verdict = review_verdicts.execute_mapping({
        "task_id": task_id,
        "pr_url": pr_url,
        "head_sha": SOURCE_HEAD,
        "reviewer_principal": REVIEWER,
        "status": "changes_requested",
        "findings": findings,
    }, actor=REVIEWER, principal_id=PRINCIPAL, project=PROJECT)
    with store._conn(PROJECT) as c:
        c.execute("UPDATE tasks SET status='Done' WHERE task_id=?", (task_id,))
    return task_id, verdict["verdict"]["verdict_id"], verdict["auto_remediation"][
        "remediation_id"]


def create_repair(source_task, source_verdict, remediation_id, *,
                  finding_ids=("FIX-1", "FIX-2"), link_status="linked",
                  pr_number=900, review_mode="adversarial", add_link=True):
    task = store.create_task(
        {"workstream_id": "BUG", "title": "BUG-172 repair"},
        actor="bug172-test", project=PROJECT)
    task_id = task["task_id"]
    with store._conn(PROJECT) as c:
        c.execute(
            "INSERT INTO task_claims("
            "id,task_id,agent_id,principal_id,status,claimed_at,expires_at,"
            "completed_at,execution_role) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"claim-{task_id}-implementation",
                task_id,
                f"codex/{task_id}-implementer",
                "principal-bug172-implementer",
                "completed",
                time.time() - 60,
                time.time() + 3600,
                time.time() - 1,
                "implementation",
            ),
        )
    store.set_agent_state(
        task_id, "bug_report",
        {"schema": "bug_report.v1", "source_task": source_task},
        project=PROJECT)
    link = {
        "schema": "switchboard.cross_task_review_repair.v1",
        "status": link_status,
        "repair_task_id": task_id,
        "source_task_id": source_task,
        "source_verdict_id": source_verdict,
        "remediation_id": remediation_id,
        "finding_ids": list(finding_ids),
    }
    if add_link:
        store.set_agent_state(task_id, "review_repair", link, project=PROJECT)
    pr_url = f"https://example.test/pull/{pr_number}"
    store.mark_task_pr_opened(
        task_id, pr_number, pr_url, branch=f"codex/{task_id}",
        head_sha=REPAIR_HEAD, actor="bug172-test", project=PROJECT)
    review_verdicts.execute_mapping({
        "task_id": task_id,
        "pr_url": pr_url,
        "head_sha": REPAIR_HEAD,
        "reviewer_principal": REVIEWER,
        "review_mode": review_mode,
        "status": "pass",
        "findings": [],
    }, actor=REVIEWER, principal_id=PRINCIPAL, project=PROJECT)
    store.append_activity("merge.gate", "bug172-test", {
        "schema": "switchboard.merge_gate.v1",
        "task_id": task_id,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "head_sha": REPAIR_HEAD,
        "status": "passed",
        "ok": True,
        "findings": [],
    }, task_id=task_id, project=PROJECT)
    return task_id, link, pr_url


def merge_repair(task_id, pr_number, pr_url, merged_sha=MERGED_SHA):
    return store.mark_task_merged(
        task_id, merged_sha, pr_number=pr_number, pr_url=pr_url,
        branch=f"codex/{task_id}", head_sha=REPAIR_HEAD,
        actor="bug172-test", project=PROJECT,
        provenance_source="github_pr_merged")


try:
    store.init_project_registry()
    store.init_db(PROJECT)

    generated, generated_failure, replayed, replay_failure = generated_state_space()
    ok(
        generated == 16384 and generated_failure is None,
        f"16,384 generated authority states fail closed ({generated_failure})",
    )
    ok(
        replayed == 4096 and replay_failure is None,
        f"4,096 post-resolution histories remain monotonic ({replay_failure})",
    )
    legacy_replay = deepcopy(valid_projection())
    legacy_replay["link"].update({
        "status": "resolved",
        "repair_head_sha": REPAIR_HEAD,
        "repair_merged_sha": MERGED_SHA,
        "repair_verdict_id": "repair-verdict",
    })
    legacy_replay["remediation"].update(
        status="resolved", resolved_head_sha=REPAIR_HEAD)
    for row in legacy_replay["source_findings"]:
        row.update(state="fixed", resolved_sha=REPAIR_HEAD)
    ok(
        classify(legacy_replay)["status"] == "resolved",
        "pre-BUG-172 terminal receipts remain readable through exact canonical "
        "git provenance",
    )

    source, verdict, remediation = create_source()
    partial, partial_link, partial_pr = create_repair(
        source, verdict, remediation, finding_ids=("FIX-1",))
    with store._conn(PROJECT) as c:
        row = c.execute(
            "SELECT acceptance_criteria_json FROM review_remediations "
            "WHERE remediation_id=?", (remediation,)).fetchone()
        criteria = json.loads(row["acceptance_criteria_json"])
        c.execute(
            "UPDATE review_remediations SET acceptance_criteria_json=?, "
            "auto_finding_count=1 WHERE remediation_id=?",
            (json.dumps(criteria[:1]), remediation))
    partial_result = merge_repair(partial, 900, partial_pr)[
        "cross_task_review_repair"]
    ok(
        partial_result["reason"] == "repair_finding_set_mismatch"
        and all(row["state"] == "open" for row in store.list_review_findings(
            task_id=source, project=PROJECT)),
        "a mutable partial remediation cannot close the canonical finding set",
    )

    source, verdict, remediation = create_source()
    cancelled, _, cancelled_pr = create_repair(
        source, verdict, remediation, link_status="cancelled", pr_number=901)
    cancelled_result = merge_repair(cancelled, 901, cancelled_pr)[
        "cross_task_review_repair"]
    ok(
        cancelled_result["reason"] == "repair_link_not_active"
        and all(row["state"] == "open" for row in store.list_review_findings(
            task_id=source, project=PROJECT)),
        "cancelled repair links are non-authoritative",
    )

    source, verdict, remediation = create_source()
    replaced, _, old_pr = create_repair(
        source, verdict, remediation, pr_number=902)
    new_pr = "https://example.test/pull/903"
    store.mark_task_pr_opened(
        replaced, 903, new_pr, branch=f"codex/{replaced}",
        head_sha=REPAIR_HEAD, actor="bug172-test", project=PROJECT)
    replaced_result = merge_repair(replaced, 903, new_pr)[
        "cross_task_review_repair"]
    ok(
        replaced_result["reason"] == "exact_pr_head_pass_required"
        and old_pr != new_pr,
        "same-head proof from a replaced PR is rejected",
    )
    fresh = review_verdicts.execute_mapping({
        "task_id": replaced,
        "pr_url": new_pr,
        "head_sha": REPAIR_HEAD,
        "reviewer_principal": REVIEWER,
        "review_mode": "adversarial",
        "status": "pass",
        "findings": [],
    }, actor=REVIEWER, principal_id=PRINCIPAL, project=PROJECT)
    store.append_activity("merge.gate", "bug172-test", {
        "schema": "switchboard.merge_gate.v1",
        "task_id": replaced,
        "pr_url": new_pr,
        "pr_number": 903,
        "head_sha": REPAIR_HEAD,
        "status": "passed",
        "ok": True,
        "findings": [],
    }, task_id=replaced, project=PROJECT)
    replacement_recovery = store.resolve_cross_task_review_repair(
        replaced, actor="bug172-test", project=PROJECT)
    with store._conn(PROJECT) as c:
        replacement_verdicts = c.execute(
            "SELECT pr_url FROM review_verdicts "
            "WHERE task_id=? AND head_sha=? ORDER BY pr_url",
            (replaced, REPAIR_HEAD),
        ).fetchall()
    ok(
        fresh["created"] is True
        and replacement_recovery["status"] == "resolved"
        and [row["pr_url"] for row in replacement_verdicts]
        == sorted([old_pr, new_pr]),
        "a replacement PR at the same SHA accepts a fresh exact-PR verdict "
        "without mutating the historical verdict",
    )

    source, verdict, remediation = create_source()
    current, _, current_pr = create_repair(
        source, verdict, remediation, pr_number=904)
    newer_same_verdict = json.dumps({
        "schema": "switchboard.review_remediation_acceptance.v1",
        "task_id": source,
        "verdict_id": verdict,
        "source_head_sha": SOURCE_HEAD,
        "round": 1,
        "requires_adversarial_review": False,
        "findings": [{"id": "NEWER-SAME-VERDICT"}],
    }, sort_keys=True)
    with store._conn(PROJECT) as c:
        c.execute(
            "UPDATE tasks SET exit_criteria=? WHERE task_id=?",
            (newer_same_verdict, source))
    merge_repair(current, 904, current_pr)
    ok(
        store.get_task(source, project=PROJECT)["exit_criteria"]
        == newer_same_verdict,
        "acceptance restoration uses an exact compare-and-swap",
    )
    store.append_activity("merge.gate", "bug172-test", {
        "schema": "switchboard.merge_gate.v1",
        "task_id": current,
        "pr_url": current_pr,
        "pr_number": 904,
        "head_sha": REPAIR_HEAD,
        "status": "blocked",
        "ok": False,
        "findings": [{"code": "late_failure", "blocking": True}],
    }, task_id=current, project=PROJECT)
    replay = store.resolve_cross_task_review_repair(
        current, actor="bug172-test", project=PROJECT)
    ok(
        replay["status"] == "resolved"
        and replay["idempotent_replay"] is True,
        "later mutable gate evidence cannot regress a committed resolution",
    )

    source, verdict, remediation = create_source(adversarial=True)
    standard, _, standard_pr = create_repair(
        source, verdict, remediation, pr_number=905, review_mode="standard")
    standard_result = merge_repair(standard, 905, standard_pr)[
        "cross_task_review_repair"]
    ok(
        standard_result["reason"] == "adversarial_review_required",
        "a standard pass cannot satisfy an adversarial repair contract",
    )

    mixed_source, mixed_verdict, mixed_remediation = create_source(mixed=True)
    mixed_task, _, mixed_pr = create_repair(
        mixed_source, mixed_verdict, mixed_remediation, pr_number=908)
    mixed_result = merge_repair(mixed_task, 908, mixed_pr)[
        "cross_task_review_repair"]
    mixed_findings = store.list_review_findings(
        task_id=mixed_source, project=PROJECT)
    mixed_summary = store.get_task(
        mixed_source, project=PROJECT)["review_remediation"]
    ok(
        mixed_result["status"] == "resolved"
        and mixed_result["human_followup_required"] is True
        and mixed_summary["current"]["status"] == "resolved_with_followup"
        and {
            row["id"]: row["state"] for row in mixed_findings
        } == {
            "FIX-1": "fixed",
            "FIX-2": "fixed",
            "HUMAN-1": "open",
        },
        "mixed findings close the exact automatic subset and preserve the "
        "human follow-up",
    )

    old_source, old_verdict, old_remediation = create_source()
    blocked_task, _, blocked_pr = create_repair(
        old_source, old_verdict, old_remediation,
        finding_ids=("FIX-1",), pr_number=906)
    merge_repair(blocked_task, 906, blocked_pr, "6" * 40)
    ready_source, ready_verdict, ready_remediation = create_source()
    ready_task, ready_link, ready_pr = create_repair(
        ready_source, ready_verdict, ready_remediation,
        pr_number=907, add_link=False)
    merge_repair(ready_task, 907, ready_pr, "7" * 40)
    store.set_agent_state(
        ready_task, "review_repair", ready_link, project=PROJECT)
    with store._conn(PROJECT) as c:
        linked_count = int(c.execute(
            "SELECT COUNT(*) FROM tasks WHERE "
            "json_extract(agent_state, '$.review_repair.status')='linked'"
        ).fetchone()[0])
    sweep_results = []
    ready_resolution = None
    for _ in range(linked_count):
        sweep = store.reconcile_cross_task_review_repairs(
            actor="bug172-test", project=PROJECT, limit=1)
        sweep_results.append(sweep["results"][0])
        if sweep["results"][0].get("repair_task_id") == ready_task:
            ready_resolution = sweep
            break
    ok(
        ready_resolution is not None
        and ready_resolution["resolved"] == 1
        and len(sweep_results) <= linked_count,
        "bounded reconciliation rotates permanent blockers and reaches ready work "
        f"(checked={len(sweep_results)}, candidates={linked_count}, "
        f"sequence={[row.get('repair_task_id') for row in sweep_results]})",
    )

    source, verdict, remediation = create_source()
    state_write_called = False

    def forbidden_state_write(*_args, **_kwargs):
        nonlocal_state[0] = True
        raise RuntimeError("old split state write must not run")

    nonlocal_state = [False]
    submitted = submit_bug.execute({
        "source_task": source,
        "source_agent": "codex/bug172-reporter",
        "observed_behavior": "Atomic intake fixture.",
        "expected_behavior": "Task and full repair contract commit together.",
        "repro_steps": "Submit an exact linked repair.",
        "evidence": {"kind": "bug172"},
        "severity_hint": "high",
        "affected_surface": "BUG intake",
        "review_repair": {
            "source_verdict_id": verdict,
            "remediation_id": remediation,
            "finding_ids": ["FIX-1", "FIX-2"],
        },
    }, actor="bug172-test", principal_id=PRINCIPAL, project=PROJECT,
        set_agent_state=forbidden_state_write,
        start_task=lambda *_args, **_kwargs: {
            "intake_routing": {"routed": True}})
    submitted_state = store.get_agent_state(
        submitted["bug"]["task_id"], project=PROJECT)
    ok(
        not nonlocal_state[0]
        and submitted_state["bug_report"]["source_task"] == source
        and submitted_state["review_repair"]["finding_ids"]
        == ["FIX-1", "FIX-2"],
        "BUG task and complete repair contract use one creation transaction",
    )
    before = len(store.list_tasks(workstream="BUG", project=PROJECT))
    try:
        store.create_task(
            {"workstream_id": "BUG", "title": "rollback fixture"},
            actor="bug172-test", project=PROJECT,
            initial_agent_state_factory=lambda _task_id: (
                (_ for _ in ()).throw(RuntimeError("factory failure"))))
    except RuntimeError:
        pass
    after = len(store.list_tasks(workstream="BUG", project=PROJECT))
    ok(
        before == after,
        "initial-state construction failure rolls back task creation",
    )
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(
    f"\nBUG-172 generated completion proof: {passed} passed, {failed} failed"
)
raise SystemExit(1 if failed else 0)
