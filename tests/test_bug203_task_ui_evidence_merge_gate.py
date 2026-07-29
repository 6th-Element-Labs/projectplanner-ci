#!/usr/bin/env python3
"""BUG-203: merge authorization reads Playwright receipts stored on the task."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug203-"))
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from switchboard.application.commands import merge_gate as merge_gate_mod  # noqa: E402


P = "switchboard"
HEAD = "9" * 40
BRANCH = "codex/BUG-203-task-ui-evidence"
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/1011"
VM_GATE = "Switchboard CI / VM gate"

store.init_db(P)
task = store.create_task(
    {
        "workstream_id": "BUG",
        "title": "Task-stored Playwright receipt",
        "ui_impact": "yes",
    },
    actor="bug203-test",
    project=P,
)
proof_session = store.create_work_session(
    {
        "task_id": task["task_id"],
        "agent_id": "agent/bug203-test",
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": BRANCH,
        "upstream": "origin/master",
        "base_sha": "8" * 40,
        "head_sha": HEAD,
        "worktree_path": str(TMP),
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
        "hygiene": {
            "repo_preflight": {"ok": True, "verdict": "pass", "findings": []}
        },
    },
    actor="bug203-test",
    project=P,
)["work_session"]
receipt = {
    "schema": "switchboard.executed_test_run.v1",
    "test_kind": "ui_playwright",
    "task_id": task["task_id"],
    "work_session_id": proof_session["work_session_id"],
    "branch": BRANCH,
    "head_sha": HEAD,
    "commands": ["python3 tests/browser/test_arch_ms126_service_boundary.py"],
    "completed_at": 1_785_201_644.0,
    "executed": True,
    "executed_count": 1,
    "skipped": False,
    "skipped_count": 0,
    "success": True,
    "exit_code": 0,
    "browser": "chromium",
    "chromium_version": "149.0.7827.55",
    "headless": True,
    "tier": "hermetic",
    "base_url": "hermetic://bug203",
    "console_errors": [],
    "console_error_count": 0,
    "failed_requests": [],
    "failed_request_count": 0,
    "output_hash": "sha256:" + "a" * 64,
    "artifact_hash": "sha256:" + "b" * 64,
}
with store._conn(P) as connection:
    store._upsert_git_state(
        connection,
        task["task_id"],
        {
            "branch": BRANCH,
            "head_sha": HEAD,
            "pr_number": 1011,
            "pr_url": PR_URL,
            "evidence": {"executed_test_runs": [receipt]},
        },
    )

result = merge_gate_mod.merge_gate(
    {
        "task_id": task["task_id"],
        "head_sha": HEAD,
        "pr_number": 1011,
        "pr_url": PR_URL,
        "github_pr": {
            "number": 1011,
            "state": "OPEN",
            "draft": False,
            "mergeable": True,
            "base": {"ref": "master"},
            "head": {"ref": BRANCH, "sha": HEAD},
            "status_contexts": [{"context": VM_GATE, "state": "SUCCESS"}],
        },
    },
    actor="bug203-test",
    project=P,
    record=False,
)
codes = {str(finding.get("code") or "") for finding in result.get("findings") or []}
if "missing_ui_playwright_evidence" in codes:
    raise SystemExit(
        "merge gate discarded task.git_state.evidence.executed_test_runs"
    )

print("PASS BUG-203 merge gate consumes task-stored Playwright evidence")
