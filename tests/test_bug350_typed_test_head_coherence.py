#!/usr/bin/env python3
"""BUG-350 — typed test evidence completes an empty bound Work Session head.

The runner can commit after its Work Session is opened.  Its typed executed-test
record is then the first authoritative observation of that head.  Recording
the evidence must make the same identity visible to complete_claim atomically,
without weakening the exact-head mismatch checks.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="bug350-test-head-coherence-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

from path_setup import ROOT  # noqa: F401,E402

import store  # noqa: E402
from switchboard.application.commands import executed_test_runs as ext_cmd  # noqa: E402

P = "switchboard"
AGENT = "agent/codex/bug350-test-head-coherence"
HEAD = "3" * 40
OTHER_HEAD = "4" * 40
GOOD_HASH = "5" * 64

store.init_db(P)
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def strict_claim(title, *, branch=None, head_sha=""):
    task = store.create_task(
        {"workstream_id": "BUG", "title": title}, actor="test", project=P)
    session_branch = f"codex/{task['task_id']}-test-head-coherence" if branch is None else branch
    claimed = store.claim_task(
        task["task_id"], AGENT,
        work_session={
            "task_id": task["task_id"],
            "agent_id": AGENT,
            "runtime": "codex",
            "repo_role": "canonical",
            "branch": session_branch,
            "upstream": "origin/master",
            "base_sha": "2" * 40,
            "head_sha": head_sha,
            "worktree_path": f"/tmp/{task['task_id'].lower()}-bug350",
            "storage_mode": "worktree",
            "status": "active",
            "dirty_status": "clean",
            "conflict_marker_count": 0,
            "policy_profile": "code_strict",
        },
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test", project=P,
    )
    ok(claimed.get("claimed") is True, f"{title}: strict claim starts")
    return task, claimed, session_branch


def record(task_id, work_session_id, *, branch, head_sha=HEAD):
    return ext_cmd.execute_mapping({
        "task_id": task_id,
        "work_session_id": work_session_id,
        "commands": ["python tests/test_bug350_typed_test_head_coherence.py"],
        "passed": True,
        "exit_code": 0,
        "output_sha256": GOOD_HASH,
        "branch": branch,
        "head_sha": head_sha,
    }, actor=AGENT, project=P)


# The production failure: branch was known, head was empty when the runner started.
task1, claim1, branch1 = strict_claim("Fill empty Work Session head")
ws1 = claim1["work_session_id"]
recorded1 = record(task1["task_id"], ws1, branch=branch1)
session1 = store.get_work_session(ws1, project=P) or {}
ok(recorded1.get("recorded") is True, "empty head: typed test record accepted")
ok(session1.get("branch") == branch1, "empty head: existing branch remains exact")
ok(session1.get("head_sha") == HEAD, "empty head: exact tested SHA copied into Work Session")
completed1 = store.complete_claim(
    claim1["claim_id"],
    evidence={
        "branch": branch1,
        "head_sha": HEAD,
        "pr_url": "https://github.example/pr/350",
        "git_diff_check": "clean",
    },
    actor=AGENT, project=P,
)
ok(completed1.get("completed") is True,
   f"empty head: complete_claim accepts the same exact SHA (reason={completed1.get('reason')})")

# Existing non-empty identity is authority: a conflicting record never overwrites it.
task2, claim2, branch2 = strict_claim("Reject conflicting Work Session head", head_sha=OTHER_HEAD)
ws2 = claim2["work_session_id"]
rejected = record(task2["task_id"], ws2, branch=branch2)
session2 = store.get_work_session(ws2, project=P) or {}
ok(rejected.get("error") == "head_sha_mismatch",
   f"conflict: different tested head rejected ({rejected.get('error')})")
ok(session2.get("head_sha") == OTHER_HEAD, "conflict: existing Work Session head is unchanged")
ok("executed_test_run" not in (session2.get("hygiene") or {}),
   "conflict: rejected evidence is not persisted")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
