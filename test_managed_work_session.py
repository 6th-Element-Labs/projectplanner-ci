#!/usr/bin/env python3
"""SESSION-7 managed Work Session workspace creation tests."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="managed-work-session-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
AGENT = "codex/SESSION-7-managed-workspaces"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def make_repo(root: Path) -> Path:
    origin = root / "origin.git"
    source = root / "source"
    git(root, "init", "--bare", str(origin))
    git(root, "clone", str(origin), str(source))
    git(source, "config", "user.email", "test@example.test")
    git(source, "config", "user.name", "Switchboard Test")
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "initial")
    git(source, "branch", "-M", "master")
    git(source, "push", "-u", "origin", "master")
    return source


def task(title):
    return store.create_task(
        {"workstream_id": "SESSION", "title": title},
        actor="test",
        project=P,
    )


try:
    root = Path(_TMP)
    source = make_repo(root)
    workspace_root = root / "workspaces"

    store.init_db(P)
    store.set_project_repo_topology(
        project=P,
        canonical_repo="example/projectplanner",
        canonical_default_branch="master",
    )
    store.register_agent(AGENT, "codex", lane="SESSION", project=P)
    contract = store.work_session_contract(P)
    ok("worktree" in contract.get("managed_workspace", {}).get("storage_modes", []) and
       "clone" in contract.get("managed_workspace", {}).get("storage_modes", []),
       "Work Session contract advertises managed workspace modes")

    worktree_task = task("managed worktree proof")
    managed = store.create_managed_work_session(
        {
            "task_id": worktree_task["task_id"],
            "agent_id": AGENT,
            "runtime": "codex",
            "source_path": str(source),
            "workspace_root": str(workspace_root),
            "storage_mode": "worktree",
            "policy_profile": "code_strict",
            "ttl_seconds": 3600,
        },
        actor="test",
        project=P,
    )
    session = managed.get("work_session") or {}
    worktree_path = Path(session.get("worktree_path") or "")
    ok(managed.get("created") is True and managed.get("managed") is True,
       "create_managed_work_session creates a managed worktree session")
    ok(worktree_path.exists() and (worktree_path / "README.md").exists(),
       "managed worktree exists on disk")
    ok(session.get("branch", "").startswith(f"codex/{worktree_task['task_id']}-") and
       session.get("storage_mode") == "worktree",
       "managed worktree receives a task-scoped branch")
    ok(session.get("dirty_status") == "clean" and session.get("conflict_marker_count") == 0 and
       session.get("hygiene", {}).get("repo_preflight", {}).get("verdict") == "pass",
       "managed worktree stores clean preflight hygiene")
    ok(bool(managed.get("session_token")) and
       managed["session_token"] not in json.dumps(session, sort_keys=True),
       "managed creation returns one-time token without storing raw token")

    claimed = store.claim_task(
        worktree_task["task_id"],
        AGENT,
        work_session_id=session["work_session_id"],
        require_work_session=True,
        session_policy_profile="code_strict",
        actor="test",
        project=P,
    )
    ok(claimed.get("claimed") is True and claimed.get("work_session_id") == session["work_session_id"],
       "strict claim can bind the managed Work Session")

    bad_mode = store.create_managed_work_session(
        {
            "task_id": task("bad mode")["task_id"],
            "agent_id": AGENT,
            "source_path": str(source),
            "workspace_root": str(workspace_root),
            "storage_mode": "external",
        },
        actor="test",
        project=P,
    )
    ok(bad_mode.get("error") == "managed_storage_mode_not_allowed",
       "managed creation rejects disallowed storage modes")

    existing_path = workspace_root / "already-there"
    existing_path.mkdir(parents=True)
    path_collision = store.create_managed_work_session(
        {
            "task_id": task("path collision")["task_id"],
            "agent_id": AGENT,
            "source_path": str(source),
            "workspace_root": str(workspace_root),
            "workspace_path": str(existing_path),
            "storage_mode": "worktree",
        },
        actor="test",
        project=P,
    )
    ok(path_collision.get("error") == "workspace_path_exists",
       "managed creation fails closed when workspace path already exists")

    clone_task = task("managed clone proof")
    clone = store.create_managed_work_session(
        {
            "task_id": clone_task["task_id"],
            "agent_id": AGENT,
            "runtime": "codex",
            "source_path": str(source),
            "workspace_root": str(workspace_root),
            "storage_mode": "clone",
            "policy_profile": "code_strict",
        },
        actor="test",
        project=P,
    )
    clone_session = clone.get("work_session") or {}
    clone_path = Path(clone_session.get("clone_path") or "")
    ok(clone.get("created") is True and clone_path.exists() and
       clone_session.get("storage_mode") == "clone",
       "managed clone mode creates a ready clone Work Session")

    archived = store.archive_work_session_workspace(
        clone_session["work_session_id"],
        remove_workspace=True,
        actor="test",
        project=P,
    )
    ok(archived.get("archived") is True and archived.get("removed_workspace") is True and
       not clone_path.exists(),
       "managed workspace archive can remove owned clone workspace")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
