#!/usr/bin/env python3
"""SESSION-1 Work Session model/API contract tests."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="work-session-model-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    task = store.create_task(
        {"workstream_id": "SESSION", "title": "work session proof"},
        actor="test",
        project=P,
    )
    store.register_agent(
        "codex/SESSION-proof",
        "codex",
        lane="SESSION",
        task_id=task["task_id"],
        project=P,
    )
    claim = store.claim_task(
        task["task_id"],
        "codex/SESSION-proof",
        actor="test",
        project=P,
    )

    created = store.create_work_session(
        {
            "task_id": task["task_id"],
            "claim_id": claim["claim_id"],
            "agent_id": "codex/SESSION-proof",
            "runtime": "codex",
            "repo_role": "canonical",
            "branch": "codex/SESSION-proof",
            "upstream": "origin/master",
            "base_sha": "abcdef1",
            "head_sha": "abcdef2",
            "worktree_path": "/tmp/projectplanner-session-proof",
            "storage_mode": "worktree",
            "status": "active",
            "dirty_status": "clean",
            "conflict_marker_count": 0,
            "hygiene": {
                "git_status": "clean",
                "conflict_marker_scan": "passed",
            },
            "file_leases": [{"path": "store.py", "lease_id": "lease-store"}],
            "resource_leases": [{"resource_type": "port", "names": ["9111"]}],
            "env": {"private_port": 9111, "live_8080_touched": False},
            "policy_profile": "code_strict",
            "session_token": "raw-token-never-stored",
        },
        actor="test",
        project=P,
    )
    session = created["work_session"]
    ok(created["created"] is True and session["schema"] == store.WORK_SESSION_SCHEMA,
       "create_work_session returns a typed Work Session")
    ok(session["project_id"] == P and session["task_id"] == task["task_id"],
       "session binds to project and task")
    ok(session["repo"] == "6th-Element-Labs/projectplanner" and
       session["default_branch"] == "master",
       "canonical repo role defaults from project repo_topology")
    ok(session["hygiene"]["git_status"] == "clean" and
       session["file_leases"][0]["path"] == "store.py" and
       session["env"]["private_port"] == 9111,
       "structured hygiene, leases, and env round-trip")
    ok(session["session_token_hash_present"] is True and "raw-token-never-stored" not in
       json.dumps(session, sort_keys=True),
       "raw session token is not returned or stored in session payload")

    listed = store.list_work_sessions(task_id=task["task_id"], agent_id="codex/SESSION-proof",
                                      status="active", project=P)
    ok(len(listed) == 1 and listed[0]["work_session_id"] == session["work_session_id"],
       "list_work_sessions filters by task, agent, and status")

    updated = store.update_work_session(
        session["work_session_id"],
        {
            "status": "completed",
            "dirty_status": "clean",
            "head_sha": "fedcba9",
            "hygiene": {"git_status": "clean", "tests": ["test_work_session_model.py"]},
        },
        actor="test",
        project=P,
    )
    ok(updated["updated"] is True and updated["work_session"]["status"] == "completed" and
       updated["work_session"]["completed_at"] is not None,
       "update_work_session records completed lifecycle")
    ok(updated["work_session"]["hygiene"]["tests"] == ["test_work_session_model.py"],
       "update_work_session replaces structured hygiene")

    contract = store.work_session_contract(P)
    ok("active" in contract["lifecycle_states"] and "worktree" in contract["storage_modes"] and
       "canonical" in contract["repo_roles"],
       "work_session_contract exposes lifecycle, storage modes, and repo roles")

    export = store.audit_export(project=P)
    export_text = json.dumps(export, sort_keys=True)
    ok(export["summary"]["work_session_count"] == 1 and
       export["work_sessions"][0]["work_session_id"] == session["work_session_id"],
       "audit export includes Work Sessions")
    ok("raw-token-never-stored" not in export_text,
       "audit export redacts raw Work Session token material")

    bad_unknown_project = store.create_work_session(
        {"agent_id": "codex/nope", "repo_role": "canonical", "worktree_path": "/tmp/nope"},
        project="missing-project",
    )
    ok(bad_unknown_project["error"] == "invalid_work_session" and
       any("unknown project" in e for e in bad_unknown_project["errors"]),
       "unknown project fails closed")

    bad_task = store.create_work_session(
        {"task_id": "NOPE-404", "agent_id": "codex/nope", "repo_role": "canonical",
         "worktree_path": "/tmp/nope"},
        project=P,
    )
    ok(any("task_id must exist" in e for e in bad_task["errors"]),
       "unknown task fails closed")

    bad_role = store.create_work_session(
        {"agent_id": "codex/nope", "repo_role": "board-local", "worktree_path": "/tmp/nope"},
        project=P,
    )
    ok(any("repo_role" in e for e in bad_role["errors"]),
       "unknown repo role fails closed")

    bad_mode = store.create_work_session(
        {"agent_id": "codex/nope", "repo_role": "canonical", "storage_mode": "shared-folder",
         "worktree_path": "/tmp/nope"},
        project=P,
    )
    ok(any("storage_mode" in e for e in bad_mode["errors"]),
       "invalid storage mode fails closed")

    missing_path = store.create_work_session(
        {"agent_id": "codex/nope", "repo_role": "canonical", "storage_mode": "worktree"},
        project=P,
    )
    ok(any("worktree_path required" in e for e in missing_path["errors"]),
       "worktree sessions require a worktree path")

    malformed = store.create_work_session(
        {"agent_id": "codex/nope", "repo_role": "canonical", "worktree_path": "/tmp/nope",
         "hygiene": "[not-json"},
        project=P,
    )
    ok(any("hygiene must be valid JSON" in e for e in malformed["errors"]),
       "malformed JSON fails closed")

    bad_count = store.create_work_session(
        {"agent_id": "codex/nope", "repo_role": "canonical", "worktree_path": "/tmp/nope",
         "conflict_marker_count": -1},
        project=P,
    )
    ok(any("conflict_marker_count" in e for e in bad_count["errors"]),
       "negative conflict marker count fails closed")

    duplicate = store.create_work_session(
        {"work_session_id": session["work_session_id"], "agent_id": "codex/duplicate",
         "repo_role": "canonical", "worktree_path": "/tmp/duplicate"},
        project=P,
    )
    ok(duplicate["error"] == "duplicate_work_session",
       "duplicate caller-supplied Work Session id fails closed")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
