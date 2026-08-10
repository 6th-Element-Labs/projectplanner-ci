#!/usr/bin/env python3
"""BUG-338: terminal retries become exact, authorized repair tasks."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

from path_setup import ROOT  # noqa: F401


TMP = tempfile.mkdtemp(prefix="bug338-terminal-review-retry-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_SQLITE_SINGLE_WRITER"] = "1"

import store  # noqa: E402
from switchboard.application.commands import review_verdicts  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402


PROJECT = "switchboard"
SOURCE_HEAD = "1" * 40
REPAIR_HEAD = "2" * 40
MERGED_SHA = "3" * 40
SOURCE_PR = "https://example.test/pull/3380"
REPAIR_PR = "https://example.test/pull/3381"
OPERATOR = "operator/bug338"
PRINCIPAL = "principal-bug338"


def finding() -> dict:
    return {
        "id": "BUG338-REPAIR-1",
        "location": "src/switchboard/application/commands/task_execution.py:1385",
        "category": "lifecycle_authority",
        "severity": "high",
        "invariant_violated": "A terminal task cannot own a live repair runner.",
        "repair_requirement": "Implement the repair in a dedicated linked task.",
        "class": "escalate",
        "state": "open",
    }


def launcher(task_id: str, **_kwargs) -> dict:
    return {
        "dispatched": True,
        "wake_id": f"wake-{task_id.lower()}",
        "runner_session_id": f"run-{task_id.lower()}",
        "host_id": "host/bug338",
    }


try:
    store.init_project_registry()
    store.init_db(PROJECT)
    source = store.create_task({
        "workstream_id": "BUG",
        "title": "terminal source with an approved repair",
    }, actor="bug338-test", project=PROJECT)
    source_id = source["task_id"]
    store.mark_task_pr_opened(
        source_id, 3380, SOURCE_PR,
        branch=f"codex/{source_id}-source", head_sha=SOURCE_HEAD,
        actor="bug338-test", project=PROJECT,
    )
    verdict = review_verdicts.execute_mapping(
        {
            "task_id": source_id,
            "pr_url": SOURCE_PR,
            "head_sha": SOURCE_HEAD,
            "reviewer_principal": "reviewer/bug338",
            "status": "changes_requested",
            "findings": [finding()],
        },
        actor="reviewer/bug338", principal_id="principal-reviewer-bug338",
        project=PROJECT,
    )
    remediation_id = verdict["auto_remediation"]["remediation_id"]
    store.mark_task_merged(
        source_id, "a" * 40, pr_number=3380, pr_url=SOURCE_PR,
        branch=f"codex/{source_id}-source", head_sha=SOURCE_HEAD,
        actor="bug338-test", project=PROJECT,
        provenance_source="github_pr_merged",
    )
    assert store.get_task(source_id, project=PROJECT)["status"] == "Done"

    direct = task_execution.execute_mapping_result(
        "start_task", source_id, project=PROJECT,
        actor=OPERATOR, principal_id=PRINCIPAL, launcher=launcher,
    )
    assert direct["error_code"] == "start_refused", direct
    assert direct["start_error"] == "terminal_task_immutable", direct
    unbound = task_execution.execute_mapping_result(
        "retry_task", source_id, project=PROJECT,
        actor=OPERATOR, principal_id="", role="remediation", launcher=launcher,
    )
    assert unbound["error_code"] == "terminal_task_requires_repair", unbound
    assert unbound["repair_error"] == "review_repair_authority_unbound", unbound

    routed = task_execution.retry_task(
        source_id, project=PROJECT, actor=OPERATOR, principal_id=PRINCIPAL,
        role="remediation", reason="operator approved exact review repair",
        launcher=launcher,
    )
    repair_id = routed["repair_task_id"]
    assert routed["action"] == "repair_routed", routed
    assert repair_id != source_id
    assert routed["started"] is True
    assert routed["execution_id"] == f"run-{repair_id.lower()}"
    assert store.get_task(source_id, project=PROJECT)["status"] == "Done"

    link = store.get_agent_state(repair_id, project=PROJECT)["review_repair"]
    assert link["source_task_id"] == source_id
    assert link["remediation_id"] == remediation_id
    assert link["finding_ids"] == ["BUG338-REPAIR-1"]
    assert link["operator_authorization"]["principal_id"] == PRINCIPAL
    assert link["operator_authorization"]["actor"] == OPERATOR

    replay = task_execution.retry_task(
        source_id, project=PROJECT, actor=OPERATOR, principal_id=PRINCIPAL,
        role="remediation", reason="operator approved exact review repair",
        launcher=launcher,
    )
    assert replay["repair_task_id"] == repair_id
    assert replay["repair_reused"] is True
    linked = [
        row for row in store.list_tasks(project=PROJECT)
        if ((row.get("agent_state") or {}).get("review_repair") or {}).get(
            "remediation_id") == remediation_id
    ]
    assert [row["task_id"] for row in linked] == [repair_id]

    store.mark_task_pr_opened(
        repair_id, 3381, REPAIR_PR,
        branch=f"codex/{repair_id}-repair", head_sha=REPAIR_HEAD,
        actor="bug338-test", project=PROJECT,
    )
    review_verdicts.execute_mapping(
        {
            "task_id": repair_id,
            "pr_url": REPAIR_PR,
            "head_sha": REPAIR_HEAD,
            "reviewer_principal": "reviewer/bug338",
            "status": "pass",
            "findings": [],
        },
        actor="reviewer/bug338", principal_id="principal-reviewer-bug338",
        project=PROJECT,
    )
    store.append_activity(
        "merge.gate", "bug338-test", {
            "schema": "switchboard.merge_gate.v1",
            "task_id": repair_id,
            "pr_url": REPAIR_PR,
            "pr_number": 3381,
            "head_sha": REPAIR_HEAD,
            "status": "passed",
            "ok": True,
            "findings": [],
        }, task_id=repair_id, project=PROJECT,
    )
    merged = store.mark_task_merged(
        repair_id, MERGED_SHA, pr_number=3381, pr_url=REPAIR_PR,
        branch=f"codex/{repair_id}-repair", head_sha=REPAIR_HEAD,
        actor="bug338-test", project=PROJECT,
        provenance_source="github_pr_merged",
    )
    resolution = merged["cross_task_review_repair"]
    assert resolution["status"] == "resolved", resolution
    assert resolution["operator_authorized"] is True
    resolved = store.list_review_findings(
        task_id=source_id, state="fixed", project=PROJECT)
    assert [row["id"] for row in resolved] == ["BUG338-REPAIR-1"]
    remediation = store.get_review_remediation(
        remediation_id, project=PROJECT)
    assert remediation["status"] == "resolved"
    assert remediation["resolved_without_human"] is False

    plain = store.create_task({
        "workstream_id": "BUG", "title": "terminal task without review repair",
    }, actor="bug338-test", project=PROJECT)
    with store._conn(PROJECT) as connection:
        connection.execute(
            "UPDATE tasks SET status='Done' WHERE task_id=?", (plain["task_id"],))
    refused = task_execution.execute_mapping_result(
        "retry_task", plain["task_id"], project=PROJECT,
        actor=OPERATOR, principal_id=PRINCIPAL, launcher=launcher,
    )
    assert refused["error_code"] == "terminal_task_requires_repair", refused
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print("BUG-338 terminal review retry repair: PASS")
