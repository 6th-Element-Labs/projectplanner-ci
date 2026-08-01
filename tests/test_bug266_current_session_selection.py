#!/usr/bin/env python3
"""BUG-266: historical failed attempts never become current again."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="bug266-session-health-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from db.connection import _conn  # noqa: E402


P = "switchboard"
store.init_db(P)


def create_session(task_id: str, suffix: str, status: str) -> str:
    result = store.create_work_session({
        "task_id": task_id,
        "agent_id": f"agent/{suffix}",
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-{suffix}",
        "upstream": f"origin/codex/{task_id}-{suffix}",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "worktree_path": str(ROOT),
        "storage_mode": "worktree",
        "status": status,
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "policy_profile": "code_strict",
        "hygiene": {
            "repo_preflight": {"ok": True, "verdict": "pass", "findings": []},
        },
    }, actor="bug266-test", project=P)
    return result["work_session"]["work_session_id"]


task = store.create_task({
    "workstream_id": "BUG",
    "title": "BUG-266 historical session selection",
    "status": "In Review",
    "ui_impact": "no",
}, actor="bug266-test", project=P)
task_id = task["task_id"]

# Generation 1 failed and was left as durable historical evidence.
old_session = create_session(task_id, "generation-1", "blocked")

# Generation 2 completed successfully afterward. With no active claim, task
# health must represent this newest attempt rather than searching backward for
# an older nonterminal row.
new_session = create_session(task_id, "generation-2", "completed")
with _conn(P) as c:
    c.execute(
        "UPDATE work_sessions SET updated_at=10 WHERE work_session_id=?",
        (old_session,),
    )
    c.execute(
        "UPDATE work_sessions SET updated_at=20 WHERE work_session_id=?",
        (new_session,),
    )

health = store.get_task(task_id, project=P)["session_health"]
assert health["session_count"] == 2
assert health["current_session_count"] == 0
assert health["unsafe_session_count"] == 0
assert health["status"] == "healthy"
assert health["latest_sessions"][0]["work_session_id"] == new_session
assert not any(
    finding.get("work_session_id") == old_session
    for finding in health["findings"]
)

# A genuinely newest unclaimed nonterminal session remains the one bounded
# cleanup signal; the fix must not hide a current orphan.
orphan_task = store.create_task({
    "workstream_id": "BUG",
    "title": "BUG-266 current orphan remains visible",
    "status": "Not Started",
    "ui_impact": "no",
}, actor="bug266-test", project=P)
orphan_id = orphan_task["task_id"]
orphan_session = create_session(orphan_id, "orphan", "blocked")
orphan_health = store.get_task(orphan_id, project=P)["session_health"]
assert orphan_health["current_session_count"] == 1
assert orphan_health["unsafe_session_count"] == 1
assert orphan_health["latest_sessions"][0]["work_session_id"] == orphan_session

print("BUG-266 current Work Session selection: passed")
