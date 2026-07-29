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
from switchboard.application.commands import merge_gate as merge_gate_mod  # noqa: E402

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
    # A later completion from another local generation branch must not outrank
    # the publication matching GitHub's live PR branch.
    c.execute(
        "INSERT INTO execution_publications("
        "publication_id,project_id,task_id,execution_id,"
        "execution_generation,repo_role,repository,default_branch,base_sha,"
        "branch,head_sha,pr_number,pr_url,scm_connection_ref,context_digest,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "execpub-bug216-late", P, TASK, "exec-bug216-late", 2,
            "canonical", "6th-Element-Labs/projectplanner", "master",
            "c" * 40, "codex/BUG-216-other-generation", "d" * 40,
            PR_NUMBER, PR_URL, "scm-switchboard", "sha256:bug216-late",
            2.0, 2.0,
        ),
    )

# The Mission Bot hydrator supplies the live GitHub PR but no cached expected
# head.  A stale task projection must not turn normal same-PR head movement into
# remediation; exact-head CI and review own freshness from here.
live_head_gate = merge_gate_mod.merge_gate(
    {
        "task_id": TASK,
        "pr_number": PR_NUMBER,
        "pr_url": PR_URL,
        "repo": "6th-Element-Labs/projectplanner",
        "github_pr": {
            "number": PR_NUMBER,
            "state": "OPEN",
            "draft": False,
            "mergeable": True,
            "base": {"ref": "master"},
            "head": {"ref": BRANCH, "sha": HEAD_B},
            "status_contexts": [{
                "context": "Switchboard CI / VM gate",
                "state": "PENDING",
            }],
        },
    },
    actor="bug218-test",
    project=P,
    record=False,
)
live_head_codes = {
    str(finding.get("code") or "")
    for finding in live_head_gate.get("findings") or []
}
assert "stale_head_sha" not in live_head_codes, live_head_gate
assert "execution_publication_event_mismatch" not in live_head_codes, live_head_gate

with store._conn(P) as c:
    c.execute(
        "DELETE FROM execution_publications WHERE publication_id=?",
        ("execpub-bug216-late",),
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

# BUG-226: the provider refresh above is normal PR progress. The merge gate
# must preserve the exact project/repository/PR/branch fence without
# reclassifying the new head as publication corruption and booting a writing
# remediator. Exact-head CI/review freshness owns HEAD_B from this point.
gate = merge_gate_mod.merge_gate(
    {
        "task_id": TASK,
        "head_sha": HEAD_B,
        "pr_number": PR_NUMBER,
        "pr_url": PR_URL,
        "repo": "6th-Element-Labs/projectplanner",
        "github_pr": {
            "number": PR_NUMBER,
            "state": "OPEN",
            "draft": False,
            "mergeable": True,
            "base": {"ref": "master"},
            "head": {"ref": BRANCH, "sha": HEAD_B},
            "status_contexts": [{
                "context": "Switchboard CI / VM gate",
                "state": "PENDING",
            }],
        },
    },
    actor="bug226-test",
    project=P,
    record=False,
)
gate_codes = {
    str(finding.get("code") or "")
    for finding in gate.get("findings") or []
}
assert "execution_publication_event_mismatch" not in gate_codes, gate

replayed = store.mark_task_pr_opened(
    TASK, PR_NUMBER, PR_URL, BRANCH, HEAD_B,
    actor="github-webhook", project=P, base_branch="master",
)
assert replayed.get("idempotent") is True, replayed
assert replayed["git_state"]["head_sha"] == HEAD_B, replayed
with store._conn(P) as c:
    event_count_after_replay = c.execute(
        "SELECT COUNT(*) AS n FROM activity "
        "WHERE task_id=? AND kind='git.pr_opened'",
        (TASK,),
    ).fetchone()["n"]
assert event_count_after_replay == 2, event_count_after_replay

delayed = store.mark_task_pr_opened(
    TASK, PR_NUMBER, PR_URL, BRANCH, HEAD_A,
    actor="github-webhook", project=P, base_branch="master",
)
assert delayed.get("idempotent") is True, delayed
assert delayed.get("reason") == "stale_provider_head_observation", delayed
assert delayed["git_state"]["head_sha"] == HEAD_B, delayed
observation = delayed["git_state"]["evidence"]["provider_head_observation"]
assert observation["sequence"] == 1, observation
assert observation["head_sha"] == HEAD_B, observation
assert observation["superseded_head_shas"] == [HEAD_A], observation
with store._conn(P) as c:
    event_count_after_delayed = c.execute(
        "SELECT COUNT(*) AS n FROM activity "
        "WHERE task_id=? AND kind='git.pr_opened'",
        (TASK,),
    ).fetchone()["n"]
assert event_count_after_delayed == event_count_after_replay, (
    event_count_after_delayed,
    event_count_after_replay,
)

wrong_branch = store.mark_task_pr_opened(
    TASK, PR_NUMBER, PR_URL, "codex/OTHER-1", "d" * 40,
    actor="github-webhook", project=P, base_branch="master",
)
assert wrong_branch["error"] == "execution_publication_event_mismatch", wrong_branch
assert store.get_task(TASK, project=P)["git_state"]["head_sha"] == HEAD_B

print("BUG-216 provider head refresh tests passed")
