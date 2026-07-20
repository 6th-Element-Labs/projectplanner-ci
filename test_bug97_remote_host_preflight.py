#!/usr/bin/env python3
"""BUG-97: host-local Work Sessions use exact Agent Host attestations."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="bug97-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
sys.path.insert(0, str(ROOT))

import store  # noqa: E402
from switchboard.storage.repositories import work_sessions as ws_repo  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


agent_host = load("bug97_agent_host", ROOT / "adapters" / "agent_host.py")
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    project = "switchboard"
    store.init_db(project)
    task = store.create_task(
        {"workstream_id": "BUG", "title": "remote preflight"},
        actor="bug97-test", project=project)
    task_id = task["task_id"]
    agent_id = f"codex/{task_id}"
    session_id = "worksession-bug97-remote"
    runner_id = "run_bug97_remote"
    host_id = "host/bug97-mac"
    remote_path = "/Users/test/Library/Application Support/Switchboard/workspaces/bug97"
    branch = f"codex/{task_id}-remote"
    head = "a" * 40
    created = store.create_work_session({
        "work_session_id": session_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "repo_role": "canonical",
        "branch": branch,
        "head_sha": head,
        "base_sha": "b" * 40,
        "worktree_path": remote_path,
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
        "policy_profile": "code_strict",
    }, actor="bug97-test", project=project)
    ok(created.get("created") is True, "remote Work Session fixture is created")

    attestation = {
        "schema": "switchboard.repo_preflight.v1",
        "attestation_schema": "switchboard.agent_host_repo_preflight.v1",
        "source": "agent_host_attestation",
        "captured_at": time.time(),
        "host_id": host_id,
        "runner_session_id": runner_id,
        "work_session_id": session_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "repo_path": remote_path,
        "branch": branch,
        "head_sha": head,
        "origin_url": "https://github.com/6th-Element-Labs/projectplanner.git",
        "upstream": f"origin/{branch}",
        "dirty": False,
        "conflict_marker_count": 0,
        "findings": [],
        "verdict": "pass",
        "ok": True,
    }
    store.upsert_runner_session({
        "runner_session_id": runner_id,
        "host_id": host_id,
        "agent_id": agent_id,
        "runtime": "codex",
        "task_id": task_id,
        "claim_id": "claim-bug97",
        "status": "running",
        "cwd": remote_path,
        "control": {"managed_process": True, "tier": "T3"},
        "metadata": {
            "wake_id": "wake-bug97",
            "work_session_id": session_id,
            "host_repo_preflight": attestation,
        },
        "heartbeat_at": time.time(),
        "heartbeat_ttl_s": 180,
    }, actor="host/bug97", project=project)
    result = store.preflight_work_session(
        session_id, actor="bug97-test", project=project,
        expected_branch=branch, expected_base_ref="origin/master")
    report = result.get("preflight") or {}
    ok(report.get("ok") is True
       and report.get("source") == "agent_host_attestation"
       and report.get("host_id") == host_id,
       "coordinator accepts a fresh exact host attestation without statting its Mac path")
    health = store.get_work_session_health(session_id, project=project)
    ok(health.get("safe") is True and health.get("status") != "unsafe",
       "host-attested preflight makes Work Session health safe")

    stale = dict(attestation, captured_at=time.time() - 1000)
    with store._conn(project) as c:
        c.execute(
            "UPDATE runner_sessions SET metadata_json=?, heartbeat_at=? "
            "WHERE runner_session_id=?",
            (json.dumps({"wake_id": "wake-bug97", "work_session_id": session_id,
                         "host_repo_preflight": stale}), time.time(), runner_id),
        )
    stale_result = store.preflight_work_session(
        session_id, actor="bug97-test", project=project, expected_branch=branch)
    stale_codes = {item.get("code") for item in
                   (stale_result.get("preflight") or {}).get("findings") or []}
    ok(stale_result.get("preflight", {}).get("ok") is False
       and "host_preflight_stale" in stale_codes,
       "stale host evidence fails closed")

    mismatched = dict(attestation, captured_at=time.time(), branch="codex/other-task")
    with store._conn(project) as c:
        c.execute(
            "UPDATE runner_sessions SET metadata_json=?, heartbeat_at=? "
            "WHERE runner_session_id=?",
            (json.dumps({"wake_id": "wake-bug97", "work_session_id": session_id,
                         "host_repo_preflight": mismatched}), time.time(), runner_id),
        )
    mismatch_result = store.preflight_work_session(
        session_id, actor="bug97-test", project=project, expected_branch=branch)
    mismatch_codes = {item.get("code") for item in
                      (mismatch_result.get("preflight") or {}).get("findings") or []}
    ok("host_preflight_branch_mismatch" in mismatch_codes,
       "mismatched host evidence fails closed")

    local_dir = tempfile.mkdtemp(prefix="bug97-local-", dir=TMP)
    local_session = store.create_work_session({
        "work_session_id": "worksession-bug97-local",
        "task_id": task_id,
        "agent_id": agent_id,
        "repo_role": "canonical",
        "branch": branch,
        "worktree_path": local_dir,
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
    }, actor="bug97-test", project=project)
    original_facade = ws_repo._store_facade
    class FakeFacade:
        def __getattr__(self, name):
            return getattr(store, name)

        @staticmethod
        def repo_preflight(path, **_kwargs):
            return {"schema": "switchboard.repo_preflight.v1", "source": "local",
                    "repo_path": path, "branch": branch, "head_sha": head,
                    "findings": [], "ok": True, "verdict": "pass"}
    ws_repo._store_facade = lambda: FakeFacade()
    try:
        local_result = store.preflight_work_session(
            local_session["work_session"]["work_session_id"],
            actor="bug97-test", project=project, expected_branch=branch)
    finally:
        ws_repo._store_facade = original_facade
    ok(local_result.get("preflight", {}).get("source") == "local",
       "coordinator-local paths retain ordinary filesystem preflight")

    snapshot = {
        "captured_at": time.time(), "cwd": remote_path, "branch": branch,
        "head_sha": head,
        "origin_url": "https://github.com/6th-Element-Labs/projectplanner.git",
        "upstream": f"origin/{branch}", "status_porcelain": "", "diff_check": "",
        "task_id": task_id, "agent_id": agent_id,
    }
    original_action = agent_host.supervisor_action
    agent_host.supervisor_action = lambda action, _runner: {
        "last_snapshot": snapshot} if action == "snapshot" else {}
    try:
        host_report = agent_host._host_repo_preflight({
            "runner_session_id": runner_id, "work_session_id": session_id,
            "task_id": task_id, "agent_id": agent_id, "cwd": remote_path,
        }, {"host_id": host_id}, {"work_session_id": session_id})
    finally:
        agent_host.supervisor_action = original_action
    ok(host_report.get("ok") is True
       and host_report.get("runner_session_id") == runner_id,
       "Agent Host produces the exact clean Git attestation carried by heartbeat")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
