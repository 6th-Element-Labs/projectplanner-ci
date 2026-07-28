#!/usr/bin/env python3
"""BUG-216: a fresh GitHub PR head supersedes stale publication projection."""
from __future__ import annotations

import os
import tempfile

tmp = tempfile.mkdtemp(prefix="bug216-head-refresh-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(tmp, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(tmp, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = os.path.join(tmp, "projects")

from path_setup import ROOT  # noqa: E402,F401

import store  # noqa: E402

P = "switchboard"
REQUESTED_TASK = "BUG-216-REPRO"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
BRANCH = "codex/BUG-216-head-refresh"
PR_NUMBER = 216
PR_URL = (
    "https://github.com/6th-Element-Labs/projectplanner/pull/216"
)

store.init_db(P)
task = store.create_task(
    {
        "task_id": REQUESTED_TASK,
        "workstream_id": "BUG",
        "title": "refresh stale provider head",
        "status": "Not Started",
        "ui_impact": "no",
    },
    actor="bug216-test",
    project=P,
)
TASK = task["task_id"]
store.mark_task_pr_opened(
    TASK, PR_NUMBER, PR_URL, BRANCH, HEAD_A,
    actor="github-webhook", project=P, base_branch="master",
)

with store._conn(P) as c:
    c.execute(
        "INSERT INTO execution_publications("
        "publication_id,project_id,task_id,execution_id,"
        "execution_generation,repo_role,repository,default_branch,base_sha,"
        "branch,head_sha,pr_number,pr_url,scm_connection_ref,context_digest,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "execpub-bug216-a", P, TASK, "exec-bug216-a", 1, "canonical",
            "6th-Element-Labs/projectplanner", "master", "c" * 40, BRANCH,
            HEAD_A, PR_NUMBER, PR_URL, "scm-switchboard",
            "sha256:bug216", 1.0, 1.0,
        ),
    )

advanced = store.mark_task_pr_opened(
    TASK, PR_NUMBER, PR_URL, BRANCH, HEAD_B,
    actor="github-webhook", project=P, base_branch="master",
)
assert advanced.get("error") is None, advanced
assert advanced["git_state"]["head_sha"] == HEAD_B, advanced
advance = advanced["git_state"]["evidence"]["provider_head_advance"]
assert advance["previous_head_sha"] == HEAD_A, advance
assert advance["current_head_sha"] == HEAD_B, advance
history = advanced["git_state"]["evidence"]["execution_publication_history"]
assert history[0]["head_sha"] == HEAD_A, history

replayed = store.mark_task_pr_opened(
    TASK, PR_NUMBER, PR_URL, BRANCH, HEAD_B,
    actor="github-webhook", project=P, base_branch="master",
)
assert replayed.get("idempotent") is True, replayed
assert replayed["git_state"]["head_sha"] == HEAD_B, replayed

wrong_branch = store.mark_task_pr_opened(
    TASK, PR_NUMBER, PR_URL, "codex/OTHER-1", "d" * 40,
    actor="github-webhook", project=P, base_branch="master",
)
assert wrong_branch["error"] == "execution_publication_event_mismatch", wrong_branch
assert store.get_task(TASK, project=P)["git_state"]["head_sha"] == HEAD_B

print("BUG-216 provider head refresh tests passed")
