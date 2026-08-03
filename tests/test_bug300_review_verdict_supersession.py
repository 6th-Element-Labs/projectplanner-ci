#!/usr/bin/env python3
"""BUG-300 — later exact-head defects supersede a pass without rewriting it."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

from path_setup import ROOT, SRC  # noqa: F401

from db.schema import apply_schema
from switchboard.application.commands import review_verdicts as commands
from switchboard.storage.migrations import runner
from switchboard.storage.repositories import review_verdicts as repository_module


PROJECT = "switchboard"
TASK_ID = "BUG-300-TEST"
HEAD = "a" * 40
PR_URL = "https://github.com/example/project/pull/1309"
API_PR_URL = "https://api.github.com/repos/example/project/pulls/1309"


def finding(finding_id: str, *, repair: str | None = None) -> dict[str, object]:
    return {
        "schema": "switchboard.review_finding.v1",
        "id": finding_id,
        "location": "src/switchboard/example.py:10",
        "category": "correctness",
        "severity": "high",
        "invariant_violated": "New exact-head defects must make the merge gate red.",
        "repair_requirement": repair or f"Repair {finding_id} before merge.",
        "class": "auto",
        "state": "open",
    }


def command(
        reviewer: str, status: str,
        findings: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "switchboard.review_verdict.record_command.v1",
        "task_id": TASK_ID,
        "pr_url": PR_URL,
        "head_sha": HEAD,
        "reviewer_principal": reviewer,
        "review_mode": "standard",
        "status": status,
        "findings": findings,
    }


class CaptureRemediation:
    def __init__(self) -> None:
        self.verdicts: list[dict[str, object]] = []

    def handle_verdict(
            self, verdict: dict[str, object], *, actor: str,
            project: str) -> dict[str, object]:
        self.verdicts.append(verdict)
        return {"status": "captured", "actor": actor, "project": project}


db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
apply_schema(db)
now = 1_785_700_000.0
db.execute(
    "INSERT INTO tasks(task_id, workstream_id, title, status, sort_order, "
    "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
    (TASK_ID, "BUG", "review supersession fixture", "In Review", 1, now, now),
)
db.execute(
    "INSERT INTO task_git_state(task_id, branch, head_sha, pushed_at, pr_number, "
    "pr_url, evidence_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
    (TASK_ID, "agent/test", HEAD, now, 1309, PR_URL, "{}", now),
)
db.commit()

repository = repository_module.ReviewVerdictRepository()
remediation = CaptureRemediation()

with (
    patch.object(repository_module, "_conn", return_value=db),
    patch.object(
        repository_module, "_write_through", side_effect=lambda _project, fn: fn()),
    patch(
        "switchboard.storage.repositories.review_remediations.required_review_mode_in",
        return_value={"required": False},
    ),
):
    first = commands.execute_mapping(
        command("agent/reviewer-a", "pass", []),
        actor="agent/reviewer-a", principal_id="principal/a", project=PROJECT,
        repository=repository, remediation_repository=remediation,
    )
    assert first["created"] is True
    assert first["verdict"]["revision"] == 1
    first_id = first["verdict"]["verdict_id"]

    replay = commands.execute_mapping(
        command("agent/reviewer-a", "pass", []),
        actor="agent/reviewer-a", principal_id="principal/a", project=PROJECT,
        repository=repository, remediation_repository=remediation,
    )
    assert replay["idempotent_replay"] is True

    defect_one = command(
        "agent/reviewer-b", "changes_requested", [finding("HOST-5-F1")])
    second = commands.execute_mapping(
        defect_one, actor="agent/reviewer-b", principal_id="principal/b",
        project=PROJECT, repository=repository,
        remediation_repository=remediation,
    )
    assert second["created"] is True and second["superseded"] is True
    assert second["superseded_verdict_id"] == first_id
    assert second["verdict"]["revision"] == 2
    assert second["verdict"]["status"] == "changes_requested"
    assert second["verdict"]["open_finding_count"] == 1
    second_id = second["verdict"]["verdict_id"]
    first_row = db.execute(
        "SELECT * FROM review_verdicts WHERE verdict_id=?", (first_id,)
    ).fetchone()
    historical_pass = repository_module._verdict_from_row(
        db, first_row, HEAD, PR_URL)
    assert historical_pass["status"] == "pass"
    assert historical_pass["valid_for_current_head"] is False
    assert historical_pass["superseded_by_verdict_id"] == second_id

    replay_defect = commands.execute_mapping(
        defect_one, actor="agent/reviewer-b", principal_id="principal/b",
        project=PROJECT, repository=repository,
        remediation_repository=remediation,
    )
    assert replay_defect["idempotent_replay"] is True
    assert replay_defect["verdict"]["verdict_id"] == second_id

    db.execute(
        "UPDATE task_git_state SET pr_url=? WHERE task_id=?", (API_PR_URL, TASK_ID))
    db.commit()
    third = commands.execute_mapping(
        command(
            "agent/reviewer-c", "changes_requested", [finding("HOST-5-F2")]),
        actor="agent/reviewer-c", principal_id="principal/c", project=PROJECT,
        repository=repository, remediation_repository=remediation,
    )
    assert third["created"] is True and third["superseded"] is True
    assert third["superseded_verdict_id"] == second_id
    assert third["verdict"]["revision"] == 3
    assert third["verdict"]["pr_url"] == API_PR_URL
    assert {item["id"] for item in third["verdict"]["findings"]} == {
        "HOST-5-F1", "HOST-5-F2",
    }
    assert remediation.verdicts[-1]["open_finding_count"] == 2

    historical_replay = commands.execute_mapping(
        defect_one, actor="agent/reviewer-b", principal_id="principal/b",
        project=PROJECT, repository=repository,
        remediation_repository=remediation,
    )
    assert historical_replay["idempotent_replay"] is True
    assert historical_replay["verdict"]["revision"] == 3

    unsafe_green = commands.execute_mapping(
        command("agent/reviewer-c", "pass", []),
        actor="agent/reviewer-c", principal_id="principal/c", project=PROJECT,
        repository=repository, remediation_repository=remediation,
    )
    assert unsafe_green["error_code"] == "review_verdict_conflict"

    altered_finding = commands.execute_mapping(
        command(
            "agent/reviewer-c", "changes_requested",
            [finding("HOST-5-F1", repair="Silently replace the original repair.")],
        ),
        actor="agent/reviewer-c", principal_id="principal/c", project=PROJECT,
        repository=repository, remediation_repository=remediation,
    )
    assert altered_finding["error_code"] == "review_finding_conflict"

    active = repository.get(TASK_ID, project=PROJECT)
    assert active is not None and active["revision"] == 3
    gate = repository_module.review_merge_gate(TASK_ID, HEAD, project=PROJECT)
    assert gate["ok"] is False and gate["code"] == "open_review_findings"
    assert gate["open_finding_count"] == 2
    open_findings = repository.list_findings(
        task_id=TASK_ID, state="open", current_head_only=True, project=PROJECT)
    assert {item["id"] for item in open_findings} == {"HOST-5-F1", "HOST-5-F2"}

    resolved_f1 = commands.resolve_finding_mapping(
        {
            "schema": "switchboard.review_finding.resolve_command.v1",
            "task_id": TASK_ID,
            "head_sha": HEAD,
            "finding_id": "HOST-5-F1",
            "state": "overridden",
            "resolved_reason": "The inherited first defect was repaired.",
            "resolved_sha": HEAD,
            "resolver_principal": "agent/remediator",
        },
        actor="agent/remediator", principal_id="principal/remediator",
        authorized=True, project=PROJECT, repository=repository,
        remediation_repository=remediation,
    )
    assert resolved_f1["resolved"] is True
    assert resolved_f1["finding"]["state"] == "overridden"
    assert resolved_f1["verdict"]["revision"] == 3
    assert resolved_f1["verdict"]["status"] == "changes_requested"
    assert resolved_f1["verdict"]["open_finding_count"] == 1
    resolved_states = {
        item["id"]: item["state"] for item in resolved_f1["verdict"]["findings"]
    }
    assert resolved_states == {"HOST-5-F1": "overridden", "HOST-5-F2": "open"}
    f1_row = db.execute(
        "SELECT verdict_id,state FROM review_findings WHERE finding_id='HOST-5-F1'"
    ).fetchone()
    assert f1_row["verdict_id"] == second_id and f1_row["state"] == "overridden"
    remaining = repository.list_findings(
        task_id=TASK_ID, state="open", current_head_only=True, project=PROJECT)
    assert [item["id"] for item in remaining] == ["HOST-5-F2"]
    gate_after_f1 = repository_module.review_merge_gate(
        TASK_ID, HEAD, project=PROJECT)
    assert gate_after_f1["ok"] is False
    assert gate_after_f1["code"] == "open_review_findings"
    assert gate_after_f1["open_finding_count"] == 1

rows = db.execute(
    "SELECT verdict_id,status,revision,supersedes_verdict_id "
    "FROM review_verdicts WHERE task_id=? ORDER BY revision",
    (TASK_ID,),
).fetchall()
assert [(row["status"], row["revision"]) for row in rows] == [
    ("pass", 1), ("changes_requested", 2), ("changes_requested", 3),
]
assert rows[0]["supersedes_verdict_id"] is None
assert rows[1]["supersedes_verdict_id"] == rows[0]["verdict_id"]
assert rows[2]["supersedes_verdict_id"] == rows[1]["verdict_id"]
assert db.execute(
    "SELECT status FROM review_verdicts WHERE verdict_id=?", (first_id,)
).fetchone()[0] == "pass"
assert db.execute(
    "SELECT COUNT(*) FROM review_findings WHERE task_id=?", (TASK_ID,)
).fetchone()[0] == 2
assert [row[0] for row in db.execute(
    "SELECT kind FROM activity WHERE task_id=? AND kind LIKE 'review.verdict_%' "
    "ORDER BY created_at, rowid",
    (TASK_ID,),
).fetchall()] == [
    "review.verdict_recorded",
    "review.verdict_superseded",
    "review.verdict_superseded",
]
assert (
    "task_id", "pr_url", "head_sha", "revision"
) in runner._review_verdict_unique_columns(db)

legacy = sqlite3.connect(":memory:")
legacy.row_factory = sqlite3.Row
legacy.execute(
    "CREATE TABLE review_verdicts("
    "verdict_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,pr_url TEXT NOT NULL,"
    "head_sha TEXT NOT NULL,reviewer_principal TEXT NOT NULL,"
    "reviewer_principal_id TEXT,review_mode TEXT NOT NULL DEFAULT 'standard',"
    "status TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'review_command',"
    "created_at REAL NOT NULL,recorded_at REAL NOT NULL,"
    "UNIQUE(task_id,pr_url,head_sha))"
)
legacy.execute(
    "INSERT INTO review_verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
    (
        "legacy-pass", TASK_ID, PR_URL, HEAD, "agent/legacy", "principal/legacy",
        "standard", "pass", "review_command", now, now,
    ),
)
legacy.commit()
assert runner._migrate_review_verdict_revisions(legacy) is True
migrated = legacy.execute(
    "SELECT * FROM review_verdicts WHERE verdict_id='legacy-pass'"
).fetchone()
assert migrated["status"] == "pass" and migrated["revision"] == 1
assert migrated["supersedes_verdict_id"] is None
assert runner._migrate_review_verdict_revisions(legacy) is False
legacy.close()

print("PASS BUG-300 append-only review verdict supersession")
