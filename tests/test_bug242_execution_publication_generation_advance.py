#!/usr/bin/env python3
"""BUG-242: a later execution branch must not block canonical merge provenance."""
from __future__ import annotations

import os
import tempfile

tmp = tempfile.mkdtemp(prefix="bug242-publication-advance-")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(tmp, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(tmp, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = os.path.join(tmp, "projects")

from path_setup import ROOT  # noqa: E402,F401

import store  # noqa: E402

P = "switchboard"
REQUESTED_TASK = "BUG-242-REPRO"
PR_NUMBER = 1242
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/1242"
BRANCH_A = "agent/switchboard/BUG-242/execlease-old-g1"
BRANCH_B = "agent/switchboard/BUG-242/execlease-new-g2"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
MERGED_SHA = "c" * 40

store.init_db(P)
task = store.create_task(
    {
        "task_id": REQUESTED_TASK,
        "workstream_id": "BUG",
        "title": "execution publication generation advance",
        "status": "In Review",
        "ui_impact": "no",
    },
    actor="bug242-test",
    project=P,
)
TASK = task["task_id"]

with store._conn(P) as c:
    c.execute(
        "INSERT INTO execution_publications("
        "publication_id,project_id,task_id,execution_id,execution_generation,"
        "repo_role,repository,default_branch,base_sha,branch,head_sha,pr_number,"
        "pr_url,scm_connection_ref,context_digest,created_at,updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "execpub-bug242", P, TASK, "exec-old", 1, "canonical",
            "6th-Element-Labs/projectplanner", "master", "d" * 40,
            BRANCH_A, HEAD_A, PR_NUMBER, PR_URL, "scm-switchboard",
            "sha256:bug242", 1.0, 1.0,
        ),
    )

opened = store.mark_task_pr_opened(
    TASK, PR_NUMBER, PR_URL, BRANCH_B, HEAD_B,
    actor="github-webhook", project=P, base_branch="master",
)
assert not opened.get("error"), opened
assert opened["git_state"]["branch"] == BRANCH_B, opened
assert opened["git_state"]["head_sha"] == HEAD_B, opened

with store._conn(P) as c:
    publication = c.execute(
        "SELECT branch, head_sha FROM execution_publications "
        "WHERE publication_id='execpub-bug242'"
    ).fetchone()
assert publication["branch"] == BRANCH_B, dict(publication)
assert publication["head_sha"] == HEAD_B, dict(publication)

merged = store.mark_task_merged(
    TASK, MERGED_SHA, pr_number=PR_NUMBER, pr_url=PR_URL,
    branch=BRANCH_B, head_sha=HEAD_B, actor="reconcile", project=P,
    provenance_source="github_pr_merged_task_reconcile",
    base_branch="master",
)
assert not merged.get("error"), merged
assert merged["status"] == "Done", merged
assert merged["git_state"]["merged_sha"] == MERGED_SHA, merged

wrong_pr = store.mark_task_merged(
    TASK, "e" * 40, pr_number=PR_NUMBER + 1,
    pr_url=(
        "https://github.com/6th-Element-Labs/projectplanner/pull/"
        f"{PR_NUMBER + 1}"
    ),
    branch=BRANCH_B, head_sha=HEAD_B, actor="reconcile", project=P,
    base_branch="master",
)
assert wrong_pr["error"] == "execution_publication_event_mismatch", wrong_pr
assert wrong_pr["accepted"] is False, wrong_pr
assert wrong_pr["provenance_written"] is False, wrong_pr
with store._conn(P) as c:
    rejection = c.execute(
        "SELECT kind FROM activity WHERE task_id=? "
        "AND kind='git.pr_merged_rejected' ORDER BY id DESC LIMIT 1",
        (TASK,),
    ).fetchone()
assert rejection is not None

print("BUG-242 execution publication generation advance tests passed")
