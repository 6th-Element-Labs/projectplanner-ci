#!/usr/bin/env python3
"""SESSION-5 complete_claim Work Session completion-gate tests."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="complete-claim-session-gate-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
AGENT = "codex/SESSION-5-complete-claim-gate"
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


def session_payload(task_id, branch=None, head_sha="head-ok", dirty="clean", conflicts=0):
    return {
        "task_id": task_id,
        "agent_id": AGENT,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": branch or f"codex/{task_id}-complete-claim-gate",
        "upstream": "origin/master",
        "base_sha": "base-ok",
        "head_sha": head_sha,
        "worktree_path": f"/tmp/{task_id.lower()}-complete-claim-gate",
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": dirty,
        "conflict_marker_count": conflicts,
        "policy_profile": "code_strict",
    }


def claim_strict(title, order=1, **session_overrides):
    created = task(title, order=order)
    payload = session_payload(created["task_id"], **session_overrides)
    claimed = store.claim_task(
        created["task_id"],
        AGENT,
        work_session=payload,
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(claimed.get("claimed") is True, f"{title}: strict claim starts")
    return created, claimed, payload


def evidence(payload, **overrides):
    base = {
        "branch": payload["branch"],
        "head_sha": payload["head_sha"],
        "pr_url": "https://github.example/pr/5",
        "tests": ["python3 test_complete_claim_work_session_gate.py"],
        "git_diff_check": "clean",
    }
    base.update(overrides)
    return base


def active_claim(claim_id):
    with store._conn(P) as c:
        row = c.execute("SELECT status FROM task_claims WHERE id=?", (claim_id,)).fetchone()
    return row["status"] if row else None


try:
    store.init_db(P)
    store.register_agent(AGENT, "codex", lane="SESSION", project=P)

    dirty_task, dirty_claim, dirty_payload = claim_strict("dirty completion blocked", order=10)
    store.update_work_session(
        dirty_claim["work_session_id"], {"dirty_status": "dirty"}, actor="test", project=P)
    dirty_done = store.complete_claim(
        dirty_claim["claim_id"], evidence=evidence(dirty_payload), actor="test", project=P)
    ok(dirty_done["completed"] is False and dirty_done["reason"] == "dirty_work_session",
       "complete_claim rejects dirty code-strict Work Session")
    ok(active_claim(dirty_claim["claim_id"]) == "active" and
       store.get_task(dirty_task["task_id"], project=P)["status"] == "In Progress",
       "blocked completion keeps claim active and task in progress")

    conflict_task, conflict_claim, conflict_payload = claim_strict(
        "conflict completion blocked", order=20)
    store.update_work_session(
        conflict_claim["work_session_id"], {"conflict_marker_count": 1},
        actor="test", project=P)
    conflict_done = store.complete_claim(
        conflict_claim["claim_id"], evidence=evidence(conflict_payload),
        actor="test", project=P)
    ok(conflict_done["completed"] is False and conflict_done["reason"] == "conflict_markers",
       "complete_claim rejects conflict markers")

    push_task, push_claim, push_payload = claim_strict("missing push blocked", order=30)
    missing_push = store.complete_claim(
        push_claim["claim_id"],
        evidence={"branch": push_payload["branch"], "head_sha": push_payload["head_sha"],
                  "tests": ["pytest"], "git_diff_check": "clean"},
        actor="test",
        project=P,
    )
    ok(missing_push["completed"] is False and
       missing_push["reason"] == "missing_push_or_review_evidence",
       "complete_claim requires PR, pushed branch, or offline evidence")

    stale_task, stale_claim, stale_payload = claim_strict("stale head blocked", order=40)
    stale_done = store.complete_claim(
        stale_claim["claim_id"],
        evidence=evidence(stale_payload, head_sha="different-head"),
        actor="test",
        project=P,
    )
    ok(stale_done["completed"] is False and stale_done["reason"] == "stale_head_sha",
       "complete_claim rejects evidence head_sha that diverges from Work Session")

    valid_task, valid_claim, valid_payload = claim_strict("valid completion allowed", order=50)
    valid_done = store.complete_claim(
        valid_claim["claim_id"], evidence=evidence(valid_payload), actor="test", project=P)
    ok(valid_done["completed"] is True and valid_done["status"] == "In Review" and
       valid_done["work_session_gate"]["required"] is True,
       "complete_claim accepts fresh clean code-strict evidence")
    completed_session = store.get_work_session(valid_claim["work_session_id"], project=P)
    ok(completed_session["status"] == "completed" and
       active_claim(valid_claim["claim_id"]) == "completed",
       "accepted completion closes the Work Session and claim")

    allowed_dirty_task, allowed_dirty_claim, allowed_dirty_payload = claim_strict(
        "explicit dirty allowance", order=60)
    store.update_work_session(
        allowed_dirty_claim["work_session_id"], {"dirty_status": "dirty"},
        actor="test", project=P)
    allowed_dirty = store.complete_claim(
        allowed_dirty_claim["claim_id"],
        evidence=evidence(
            allowed_dirty_payload,
            allow_dirty=True,
            allow_dirty_reason="docs-only generated file remains uncommitted by policy",
        ),
        actor="test",
        project=P,
    )
    ok(allowed_dirty["completed"] is True,
       "complete_claim allows dirty state only with explicit allowance evidence")

    offline_task = task("offline non-code completion", order=70)
    offline_claim = store.claim_task(offline_task["task_id"], AGENT, actor="test", project=P)
    offline_done = store.complete_claim(
        offline_claim["claim_id"],
        evidence={"completion_profile": "offline_evidence",
                  "verification": "reviewed attached run log",
                  "artifact_url": "https://example.test/run-log"},
        actor="test",
        project=P,
    )
    ok(offline_done["completed"] is True and offline_done["status"] == "In Review" and
       offline_done["work_session_gate"]["required"] is False,
       "explicit offline profile remains compatible for non-code work")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
