#!/usr/bin/env python3
"""BUG-115: server preflight must not false-deny host-local direct-CLI Work Sessions.

A hands-off Mac dispatch (start_task -> direct_codex_session) creates a Work
Session whose worktree lives on the enrolled host, then runs preflight before the
Agent Host heartbeat has had a chance to attach its signed ``host_repo_preflight``
attestation (BUG-97). Previously the server fell back to ``repo_preflight``, which
``stat()``s a path that is definitionally not on its filesystem and emits a hard
``worktree_missing`` deny -- so every direct personal-host session was born blocked.

The fix: while a live host runner owns the session but has not attested yet, the
server returns a visible, NON-blocking "awaiting host attestation" pending report
instead of statting the remote path. BUG-159 extends that truthfulness to every
workspace outside the coordinator-managed workspace root, regardless of runner
liveness. Missing coordinator-managed workspaces still fail closed.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="bug115-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_WORKSPACE_ROOT"] = os.path.join(TMP, "managed-workspaces")
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


REMOTE = "/Users/steveridder/Library/Application Support/SwitchboardAgentHost/workspaces/bug115"
HEAD = "c" * 40


def _make_session(project, task_id, agent_id, session_id, principal_id,
                  worktree_path=REMOTE):
    return store.create_work_session({
        "work_session_id": session_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "principal_id": principal_id,
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-direct",
        "head_sha": HEAD,
        "base_sha": HEAD,
        "worktree_path": worktree_path,
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
    }, actor="bug115-test", project=project)


def _live_runner(project, task_id, agent_id, runner_id, *, work_session_id="",
                 heartbeat_age=0.0):
    metadata = {"wake_id": "wake-bug115", "direct_assignment": True}
    if work_session_id:
        metadata["work_session_id"] = work_session_id
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": "host/steve-mbp-co16",
        "agent_id": agent_id,
        "runtime": "codex",
        "task_id": task_id,
        "claim_id": "claim-bug115",
        "status": "running",
        "cwd": REMOTE,
        "control": {"managed_process": True, "tier": "T3"},
        "metadata": metadata,
        "heartbeat_at": time.time() - heartbeat_age,
        "heartbeat_ttl_s": 180,
    }, actor="host/bug115", project=project)


def _verdict(result):
    return (result.get("preflight") or {}).get("verdict")


def _codes(result):
    return {item.get("code") for item in
            (result.get("preflight") or {}).get("findings") or []}


try:
    project = "switchboard"
    store.init_db(project)

    # --- Case 1: direct-session principal binding, no attestation yet ----------
    task = store.create_task({"workstream_id": "BUG", "title": "direct pending A"},
                             actor="bug115-test", project=project)
    task_id = task["task_id"]
    agent_id = f"codex/{task_id}"
    runner_id = "run_bug115_a"
    session_id = "worksession-bug115-a"
    _make_session(project, task_id, agent_id, session_id,
                  principal_id=f"direct-session/{runner_id}")
    # Live runner exists (heartbeating) but has NOT late-bound work_session_id and
    # carries no host_repo_preflight attestation -- the real race-window state.
    _live_runner(project, task_id, agent_id, runner_id)
    res = store.preflight_work_session(session_id, actor="bug115-test", project=project)
    ok(_verdict(res) != "deny" and "worktree_missing" not in _codes(res),
       "live host runner (direct-session principal) is not false-denied worktree_missing")
    ok((res.get("preflight") or {}).get("source") == "agent_host_pending"
       and "host_preflight_pending" in _codes(res),
       "server returns a visible 'awaiting host attestation' pending report")
    pending_finding = next((f for f in (res.get("preflight") or {}).get("findings") or []
                            if f.get("code") == "host_preflight_pending"), {})
    ok(pending_finding.get("blocking") is False,
       "pending attestation is non-blocking so the Work Session stays active")
    health = store.get_work_session_health(session_id, project=project)
    ok(health.get("safe") is True and health.get("status") != "unsafe",
       "host-local direct session is not born unsafe/blocked while attestation is pending")

    # --- Case 2: work_session_id late-bound onto runner, still no attestation --
    task2 = store.create_task({"workstream_id": "BUG", "title": "direct pending B"},
                              actor="bug115-test", project=project)
    task2_id = task2["task_id"]
    agent2 = f"codex/{task2_id}"
    runner2 = "run_bug115_b"
    session2 = "worksession-bug115-b"
    _make_session(project, task2_id, agent2, session2, principal_id="direct-session/other")
    _live_runner(project, task2_id, agent2, runner2, work_session_id=session2)
    res2 = store.preflight_work_session(session2, actor="bug115-test", project=project)
    ok(_verdict(res2) != "deny" and "worktree_missing" not in _codes(res2),
       "late-bound work_session_id (no attestation) is not false-denied")
    ok((res2.get("preflight") or {}).get("source") == "agent_host_pending",
       "work_session_id-bound live runner also yields a pending report")

    # --- Case 3: no runner cannot make an external path observable -------------
    task3 = store.create_task({"workstream_id": "BUG", "title": "external unknown"},
                              actor="bug115-test", project=project)
    task3_id = task3["task_id"]
    agent3 = f"codex/{task3_id}"
    session3 = "worksession-bug115-c"
    _make_session(project, task3_id, agent3, session3,
                  principal_id=f"direct-session/run_bug115_c")
    # No runner registered for this task at all.
    res3 = store.preflight_work_session(session3, actor="bug115-test", project=project)
    ok(_verdict(res3) == "warn"
       and "work_session_preflight_unverifiable" in _codes(res3)
       and "worktree_missing" not in _codes(res3),
       "external worktree with no live runner is reported unverifiable, not missing")
    health3 = store.get_work_session_health(session3, project=project)
    ok(health3.get("safe") is True and health3.get("status") == "warning",
       "unverifiable external worktree leaves Work Session warning, not unsafe")

    # --- Case 4: stale runner does not change path observability ---------------
    task4 = store.create_task({"workstream_id": "BUG", "title": "stale runner"},
                              actor="bug115-test", project=project)
    task4_id = task4["task_id"]
    agent4 = f"codex/{task4_id}"
    runner4 = "run_bug115_d"
    session4 = "worksession-bug115-d"
    _make_session(project, task4_id, agent4, session4,
                  principal_id=f"direct-session/{runner4}")
    _live_runner(project, task4_id, agent4, runner4, heartbeat_age=10_000)
    res4 = store.preflight_work_session(session4, actor="bug115-test", project=project)
    ok(_verdict(res4) == "warn"
       and "work_session_preflight_unverifiable" in _codes(res4)
       and "worktree_missing" not in _codes(res4),
       "stale runner does not turn an unobservable external path into missing")

    # --- Case 5: missing coordinator-managed worktree still fails closed ------
    task5 = store.create_task({"workstream_id": "BUG", "title": "managed missing"},
                              actor="bug115-test", project=project)
    task5_id = task5["task_id"]
    agent5 = f"codex/{task5_id}"
    session5 = "worksession-bug159-managed"
    managed_missing = os.path.join(os.environ["PM_WORKSPACE_ROOT"], "missing")
    _make_session(project, task5_id, agent5, session5,
                  principal_id="env-mcp-token", worktree_path=managed_missing)
    res5 = store.preflight_work_session(session5, actor="bug115-test", project=project)
    ok(_verdict(res5) == "deny" and "worktree_missing" in _codes(res5),
       "missing coordinator-managed worktree still fails closed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
