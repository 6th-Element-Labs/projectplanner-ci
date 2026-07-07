#!/usr/bin/env python3
"""SESSION-4 pre_tool_check Work Session enforcement tests."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="pre-tool-check-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "required"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError:
    TestClient = None
    app = None

P = "switchboard"
AGENT = "codex/SESSION-4-pre-tool-check"
TOKEN = "pre-tool-token"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def activity_kinds(task_id):
    with store._conn(P) as c:
        return [row["kind"] for row in c.execute(
            "SELECT kind FROM activity WHERE task_id=? ORDER BY id", (task_id,))]


def make_task(title):
    return store.create_task({"workstream_id": "SESSION", "title": title},
                             actor="test", project=P)


def make_session(task_id, dirty="clean", conflicts=0):
    root = Path(_TMP) / task_id.lower()
    root.mkdir(parents=True, exist_ok=True)
    return store.create_work_session({
        "task_id": task_id,
        "agent_id": AGENT,
        "runtime": "codex",
        "repo_role": "canonical",
        "branch": f"codex/{task_id}-pre-tool-check",
        "upstream": "origin/master",
        "base_sha": "95710e1",
        "head_sha": "95710e2",
        "worktree_path": str(root),
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": dirty,
        "conflict_marker_count": conflicts,
        "policy_profile": "code_strict",
    }, actor="test", project=P)["work_session"]


try:
    store.init_db(P)
    store.register_agent(AGENT, "codex", lane="SESSION", project=P)

    clean_task = make_task("clean pre-tool session allows writes")
    clean_session = make_session(clean_task["task_id"])
    allowed = store.pre_tool_check({
        "task_id": clean_task["task_id"],
        "agent_id": AGENT,
        "work_session_id": clean_session["work_session_id"],
        "tool_name": "Edit",
        "tool_input": {"file_path": str(Path(clean_session["worktree_path"]) / "store.py")},
    }, actor="test", project=P)
    ok(allowed["decision"] == "allow" and allowed["ok"] is True,
       "clean active Work Session allows file write")

    missing_task = make_task("missing session denies side effect")
    missing = store.pre_tool_check({
        "task_id": missing_task["task_id"],
        "agent_id": AGENT,
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin codex/SESSION-4-pre-tool-check"},
    }, actor="test", project=P)
    ok(missing["decision"] == "deny" and missing["failure_class"] == "missing_data",
       "missing Work Session denies git side effect")
    ok("work_session.unsafe_session" in activity_kinds(missing_task["task_id"]),
       "missing Work Session records unsafe_session activity")

    dirty_task = make_task("dirty session denies side effect")
    dirty_session = make_session(dirty_task["task_id"], dirty="dirty")
    dirty = store.pre_tool_check({
        "task_id": dirty_task["task_id"],
        "agent_id": AGENT,
        "work_session_id": dirty_session["work_session_id"],
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr create --fill"},
    }, actor="test", project=P)
    ok(dirty["decision"] == "deny" and dirty["failure_class"] == "failed_gate",
       "dirty Work Session denies PR side effect")
    ok("work_session.unsafe_session" in activity_kinds(dirty_task["task_id"]),
       "dirty Work Session records unsafe_session activity")

    unbound_task = make_task("unbound shared token denies side effect")
    unbound = store.pre_tool_check({
        "task_id": unbound_task["task_id"],
        "tool_name": "Edit",
        "tool_input": {"file_path": "store.py"},
    }, actor="env-mcp-token", principal_id="env-mcp-token", project=P)
    ok(unbound["decision"] == "deny" and unbound["failure_class"] == "unbound_identity",
       "unbound shared principal denies side effect")
    ok("principal.unbound_write" in activity_kinds(unbound_task["task_id"]),
       "unbound shared principal records unbound_write activity")

    lease_task = make_task("file lease conflict denies write")
    lease_session = make_session(lease_task["task_id"])
    store.claim_resources("claude/SESSION-4-other", "file", ["store.py"],
                          task_id=lease_task["task_id"], project=P)
    lease = store.pre_tool_check({
        "task_id": lease_task["task_id"],
        "agent_id": AGENT,
        "work_session_id": lease_session["work_session_id"],
        "tool_name": "Write",
        "tool_input": {"file_path": str(Path(lease_session["worktree_path"]) / "store.py")},
    }, actor="test", project=P)
    ok(lease["decision"] == "deny" and lease["target_path"] == "store.py",
       "file leased by another agent denies write")

    read = store.pre_tool_check({
        "tool_name": "search_tasks",
        "action": "read",
    }, actor="test", project=P)
    ok(read["decision"] == "allow",
       "read/noop tool does not require Work Session")

    if TestClient is None:
        print("  SKIP  REST pre_tool_check smoke requires FastAPI TestClient")
    else:
        store.create_principal(
            kind="agent",
            display_name=AGENT,
            token=TOKEN,
            scopes=["read", "write:ixp"],
            project=P,
        )
        client = TestClient(app)
        rest = client.post("/ixp/v1/pre_tool_check", json={
            "project": P,
            "task_id": clean_task["task_id"],
            "agent_id": AGENT,
            "work_session_id": clean_session["work_session_id"],
            "tool_name": "Edit",
            "tool_input": {"file_path": str(Path(clean_session["worktree_path"]) / "app.py")},
        }, headers={"Authorization": f"Bearer {TOKEN}"})
        ok(rest.status_code == 200 and rest.json()["decision"] == "allow",
           "REST pre_tool_check returns allow for valid session")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
