#!/usr/bin/env python3
"""SESSION-2 Work Session claim binding tests."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="work-session-claim-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
AGENT = "codex/SESSION-2-claim-session-gate"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def task(title, order=1):
    return store.create_task(
        {"workstream_id": "SESSION", "title": title, "sort_order": order},
        actor="test",
        project=P,
    )


def session_payload(task_id, branch=None, dirty="clean", conflicts=0):
    return {
        "task_id": task_id,
        "agent_id": AGENT,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": branch or f"codex/{task_id}-claim-session-gate",
        "upstream": "origin/master",
        "base_sha": "0ab2e96",
        "worktree_path": f"/tmp/{task_id.lower()}-claim-session-gate",
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": dirty,
        "conflict_marker_count": conflicts,
        "policy_profile": "code_strict",
    }


try:
    store.init_db(P)
    store.register_agent(AGENT, "codex", lane="SESSION", project=P)

    missing_task = task("strict claim requires Work Session", order=10)
    missing = store.claim_task(
        missing_task["task_id"],
        AGENT,
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(missing["claimed"] is False and missing["reason"] == "work_session_required" and
       missing["failure_class"] == "missing_data",
       "claim_task fails closed when strict Work Session is missing")

    dirty_task = task("strict claim rejects dirty Work Session", order=20)
    dirty = store.claim_task(
        dirty_task["task_id"],
        AGENT,
        work_session=session_payload(dirty_task["task_id"], dirty="dirty"),
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(dirty["claimed"] is False and dirty["reason"] == "dirty_work_session" and
       dirty["failure_class"] == "failed_gate",
       "claim_task rejects dirty strict Work Session")

    wrong_branch_task = task("strict claim rejects wrong branch", order=30)
    wrong_branch = store.claim_task(
        wrong_branch_task["task_id"],
        AGENT,
        work_session=session_payload(wrong_branch_task["task_id"], branch="codex/not-this-task"),
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(wrong_branch["claimed"] is False and wrong_branch["reason"] == "wrong_branch" and
       wrong_branch["failure_class"] == "stale_branch",
       "claim_task rejects a branch that is not task-scoped")

    ok_task = task("strict claim binds Work Session", order=40)
    claimed = store.claim_task(
        ok_task["task_id"],
        AGENT,
        work_session=session_payload(ok_task["task_id"]),
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(claimed["claimed"] is True and claimed["work_session_id"] and
       claimed["work_session"]["status"] == "bound",
       "claim_task returns a bound work_session_id")
    bound_session = store.get_work_session(claimed["work_session_id"], project=P)
    ok(bound_session["claim_id"] == claimed["claim_id"] and
       bound_session["task_id"] == ok_task["task_id"] and
       bound_session["agent_id"] == AGENT,
       "bound Work Session records claim/task/agent ownership")
    ok(claimed["dispatch_reason"]["work_session"]["required"] is True and
       claimed["dispatch_reason"]["work_session"]["policy_profile"] == "code_strict",
       "dispatch_reason includes Work Session gate evidence")

    skip_task = task("claim_next skips unsafe Work Session", order=50)
    skipped = store.claim_next(
        AGENT,
        lanes=["SESSION"],
        work_session=session_payload(skip_task["task_id"], dirty="dirty"),
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(skipped["claimed"] is False and
       skipped["dispatch_reason"]["skipped"]["work_session"] >= 1 and
       skip_task["task_id"] in skipped["dispatch_reason"]["work_session_findings"],
       "claim_next skips strict candidates with unsafe Work Sessions")

    next_task = task("claim_next binds Work Session", order=60)
    next_claim = store.claim_next(
        AGENT,
        lanes=["SESSION"],
        work_session=session_payload(next_task["task_id"]),
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(next_claim["claimed"] is True and next_claim["task"]["task_id"] == next_task["task_id"] and
       next_claim["work_session_id"],
       "claim_next returns a bound work_session_id")

    legacy_task = task("legacy advisory claim still works", order=70)
    legacy = store.claim_task(legacy_task["task_id"], AGENT, actor="test", project=P)
    ok(legacy["claimed"] is True and legacy["work_session_id"] is None,
       "non-strict legacy claim remains compatible")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
