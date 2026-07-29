#!/usr/bin/env python3
"""BUG-234 — API and browser URLs must resolve to one review-verdict identity."""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

from path_setup import ROOT, SRC  # noqa: F401

from db.schema import apply_schema
from switchboard.storage.repositories import review_verdicts as review_repo


TASK_ID = "BUG-234-TEST"
HEAD = "a" * 40
BROWSER_URL = "https://github.com/example/project/pull/810"
API_URL = "https://api.github.com/repos/example/project/pulls/810"


db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
apply_schema(db)
now = 1_785_000_000.0
db.execute(
    "INSERT INTO tasks(task_id, workstream_id, title, status, sort_order, "
    "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
    (TASK_ID, "BUG", "review identity fixture", "In Review", 1, now, now),
)
db.execute(
    "INSERT INTO task_git_state(task_id, branch, head_sha, pushed_at, pr_number, "
    "pr_url, evidence_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
    (TASK_ID, "codex/bug-234", HEAD, now, 810, BROWSER_URL, "{}", now),
)
db.commit()

repository = review_repo.ReviewVerdictRepository()
payload = {
    "task_id": TASK_ID,
    "pr_url": API_URL,
    "head_sha": HEAD,
    "reviewer_principal": "agent/test",
    "review_mode": "standard",
    "status": "pass",
    "findings": [],
}

with (
    patch.object(review_repo, "_conn", return_value=db),
    patch.object(review_repo, "_write_through", side_effect=lambda _project, fn: fn()),
    patch(
        "switchboard.storage.repositories.review_remediations.required_review_mode_in",
        return_value={"required": False},
    ),
):
    recorded = repository.record(
        payload, actor="agent/test", principal_id="principal/test",
        project="switchboard",
    )
    fetched = repository.get(
        TASK_ID, head_sha=HEAD, pr_url=API_URL, project="switchboard",
    )

assert recorded["created"] is True
assert recorded["verdict"]["pr_url"] == BROWSER_URL
assert recorded["verdict"]["valid_for_current_head"] is True
assert fetched is not None and fetched["status"] == "pass"
assert fetched["valid_for_current_head"] is True
assert db.execute(
    "SELECT COUNT(*) FROM review_verdicts WHERE task_id=?", (TASK_ID,)
).fetchone()[0] == 1

print("PASS BUG-234 canonical review PR identity")
