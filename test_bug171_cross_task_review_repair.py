#!/usr/bin/env python3
"""BUG-171: a canonical repair task closes only its explicitly linked findings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile


TMP = tempfile.mkdtemp(prefix="bug171-cross-task-review-repair-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SQLITE_SINGLE_WRITER"] = "1"

import store  # noqa: E402
from switchboard.application.commands import submit_bug  # noqa: E402
from switchboard.application.commands import review_verdicts  # noqa: E402


PROJECT = "switchboard"
REVIEWER = "codex/bug171-reviewer"
REVIEWER_PRINCIPAL = "principal-bug171-reviewer"
SOURCE_HEAD = "1" * 40
REPAIR_HEAD = "2" * 40
MERGED_SHA = "3" * 40
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def finding(finding_id):
    return {
        "id": finding_id,
        "location": "src/switchboard/domain/example.py:42",
        "category": "routing",
        "severity": "high",
        "invariant_violated": "Only reviewed canonical repair proof closes a finding.",
        "repair_requirement": "Implement and independently verify the exact repair.",
        "class": "auto",
        "state": "open",
    }


def create_source():
    task = store.create_task(
        {"workstream_id": "COORD", "title": "BUG-171 source fixture",
         "exit_criteria": "Original source acceptance"},
        actor="bug171-test", project=PROJECT,
    )
    task_id = task["task_id"]
    store.mark_task_pr_opened(
        task_id, 900, "https://example.test/pull/900",
        branch=f"codex/{task_id}", head_sha=SOURCE_HEAD,
        actor="bug171-test", project=PROJECT,
    )
    verdict = review_verdicts.execute_mapping(
        {
            "task_id": task_id,
            "pr_url": "https://example.test/pull/900",
            "head_sha": SOURCE_HEAD,
            "reviewer_principal": REVIEWER,
            "status": "changes_requested",
            "findings": [finding("FIX-1"), finding("FIX-2")],
        },
        actor=REVIEWER,
        principal_id=REVIEWER_PRINCIPAL,
        project=PROJECT,
    )
    remediation = verdict["auto_remediation"]
    with store._conn(PROJECT) as c:
        c.execute(
            "UPDATE tasks SET status='Done' WHERE task_id=?",
            (task_id,),
        )
    return task_id, verdict["verdict"]["verdict_id"], remediation["remediation_id"]


def create_repair(source_task, verdict_id, remediation_id, finding_ids):
    task = store.create_task(
        {"workstream_id": "BUG", "title": "BUG-171 repair fixture"},
        actor="bug171-test", project=PROJECT,
    )
    task_id = task["task_id"]
    store.set_agent_state(
        task_id, "bug_report",
        {"schema": "bug_report.v1", "source_task": source_task},
        project=PROJECT,
    )
    store.set_agent_state(
        task_id, "review_repair",
        {
            "schema": "switchboard.cross_task_review_repair.v1",
            "status": "linked",
            "repair_task_id": task_id,
            "source_task_id": source_task,
            "source_verdict_id": verdict_id,
            "remediation_id": remediation_id,
            "finding_ids": finding_ids,
        },
        project=PROJECT,
    )
    store.mark_task_pr_opened(
        task_id, 901, "https://example.test/pull/901",
        branch=f"codex/{task_id}", head_sha=REPAIR_HEAD,
        actor="bug171-test", project=PROJECT,
    )
    review_verdicts.execute_mapping(
        {
            "task_id": task_id,
            "pr_url": "https://example.test/pull/901",
            "head_sha": REPAIR_HEAD,
            "reviewer_principal": REVIEWER,
            "status": "pass",
            "findings": [],
        },
        actor=REVIEWER,
        principal_id=REVIEWER_PRINCIPAL,
        project=PROJECT,
    )
    store.append_activity(
        "merge.gate", "bug171-test",
        {
            "schema": "switchboard.merge_gate.v1",
            "task_id": task_id,
            "pr_url": "https://example.test/pull/901",
            "pr_number": 901,
            "head_sha": REPAIR_HEAD,
            "status": "passed",
            "ok": True,
            "findings": [],
        },
        task_id=task_id,
        project=PROJECT,
    )
    return task_id


try:
    store.init_project_registry()
    store.init_db(PROJECT)

    source_task, source_verdict, remediation_id = create_source()
    repair_task = create_repair(
        source_task, source_verdict, remediation_id, ["FIX-1", "FIX-2"])
    merged = store.mark_task_merged(
        repair_task, MERGED_SHA, pr_number=901,
        pr_url="https://example.test/pull/901",
        branch=f"codex/{repair_task}", head_sha=REPAIR_HEAD,
        actor="bug171-test", project=PROJECT,
        provenance_source="github_pr_merged",
    )
    findings = store.list_review_findings(
        task_id=source_task, project=PROJECT)
    remediation = store.get_review_remediation(
        remediation_id, project=PROJECT)
    source_detail = store.get_task(source_task, project=PROJECT)
    repair_state = store.get_agent_state(repair_task, project=PROJECT)
    resolution = merged.get("cross_task_review_repair") or {}

    ok(
        resolution.get("status") == "resolved"
        and resolution.get("repair_head_sha") == REPAIR_HEAD
        and resolution.get("repair_merged_sha") == MERGED_SHA,
        "merge provenance immediately resolves the explicitly linked repair",
    )
    ok(
        all(row["state"] == "fixed" and row["resolved_sha"] == REPAIR_HEAD
            for row in findings),
        "only exact linked source findings are marked fixed at the repair head",
    )
    ok(
        remediation["status"] == "resolved"
        and remediation["resolved_head_sha"] == REPAIR_HEAD
        and remediation["resolved_without_human"] is True,
        "the source remediation records hands-off canonical repair proof",
    )
    ok(
        source_detail["status"] == "Done"
        and source_detail["exit_criteria"] == "Original source acceptance",
        "historical source task terminal state is preserved and acceptance is restored",
    )
    ok(
        repair_state["review_repair"]["repair_verdict_id"]
        and repair_state["review_repair"]["repair_merged_sha"] == MERGED_SHA,
        "the repair task keeps the durable cross-task resolution receipt",
    )
    source_verdict_detail = store.get_review_verdict(
        source_task, project=PROJECT, head_sha=SOURCE_HEAD)
    ok(
        source_verdict_detail["status"] == "changes_requested",
        "the failed source-head verdict remains auditable",
    )

    replay = store.resolve_cross_task_review_repair(
        repair_task, actor="bug171-test", project=PROJECT)
    ok(
        replay.get("status") == "resolved"
        and replay.get("idempotent_replay") is True,
        "resolution replay is idempotent",
    )

    advanced_source, advanced_verdict, advanced_remediation = create_source()
    advanced_repair = create_repair(
        advanced_source, advanced_verdict, advanced_remediation, ["FIX-1", "FIX-2"])
    newer_acceptance = json.dumps(
        {
            "schema": "switchboard.review_remediation_acceptance.v1",
            "verdict_id": "reviewverdict-newer-round",
            "acceptance_criteria": [{"id": "NEW-FIX"}],
        },
        sort_keys=True,
    )
    with store._conn(PROJECT) as c:
        c.execute(
            "UPDATE tasks SET exit_criteria=? WHERE task_id=?",
            (newer_acceptance, advanced_source),
        )
    store.mark_task_merged(
        advanced_repair, "5" * 40, pr_number=901,
        pr_url="https://example.test/pull/901",
        branch=f"codex/{advanced_repair}", head_sha=REPAIR_HEAD,
        actor="bug171-test", project=PROJECT,
        provenance_source="github_pr_merged",
    )
    advanced_detail = store.get_task(advanced_source, project=PROJECT)
    ok(
        advanced_detail["exit_criteria"] == newer_acceptance,
        "an older repair cannot overwrite a newer source acceptance contract",
    )

    bad_source, bad_verdict, bad_remediation = create_source()
    bad_repair = create_repair(
        bad_source, bad_verdict, bad_remediation, ["FIX-1"])
    bad_merge = store.mark_task_merged(
        bad_repair, "4" * 40, pr_number=901,
        pr_url="https://example.test/pull/901",
        branch=f"codex/{bad_repair}", head_sha=REPAIR_HEAD,
        actor="bug171-test", project=PROJECT,
        provenance_source="github_pr_merged",
    )
    bad_findings = store.list_review_findings(
        task_id=bad_source, project=PROJECT)
    retry = store.reconcile_cross_task_review_repairs(
        actor="bug171-test", project=PROJECT)
    ok(
        bad_merge["cross_task_review_repair"]["reason"]
        == "repair_finding_set_mismatch"
        and retry["blocked"] == 1
        and all(row["state"] == "open" for row in bad_findings),
        "reconcile retries a partial finding set but fails closed without mutation",
    )

    submitted = submit_bug.execute(
        {
            "source_task": bad_source,
            "source_agent": "codex/bug171-reporter",
            "observed_behavior": "A linked review finding needs a dedicated repair task.",
            "expected_behavior": "The repair task carries an exact source contract.",
            "repro_steps": "Record the source verdict and submit its dedicated repair.",
            "evidence": {"remediation_id": bad_remediation},
            "severity_hint": "high",
            "affected_surface": "cross-task review repair intake",
            "review_repair": {
                "source_verdict_id": bad_verdict,
                "remediation_id": bad_remediation,
                "finding_ids": ["FIX-1", "FIX-2"],
            },
        },
        actor="bug171-test",
        principal_id="principal-bug171-test",
        project=PROJECT,
        start_task=lambda *_args, **_kwargs: {
            "intake_routing": {"routed": True},
        },
    )
    submitted_state = store.get_agent_state(
        submitted["bug"]["task_id"], project=PROJECT)
    ok(
        submitted_state["review_repair"]["repair_task_id"]
        == submitted["bug"]["task_id"]
        and submitted_state["review_repair"]["source_task_id"] == bad_source
        and submitted_state["review_repair"]["finding_ids"] == ["FIX-1", "FIX-2"],
        "bug intake persists the exact cross-task repair contract before dispatch",
    )
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nBUG-171 cross-task repair: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
