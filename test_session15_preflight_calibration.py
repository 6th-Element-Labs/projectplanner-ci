#!/usr/bin/env python3
"""SESSION-15: preflight prediction→outcome learning loop."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="session-15-preflight-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
from switchboard.storage.repositories import preflight_runs as pf  # noqa: E402

P = "switchboard"
AGENT = "codex/SESSION-15-learn"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def run(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(repo), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def commit(repo, name, text, message=None):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    run(repo, "add", name)
    run(repo, "commit", "-m", message or f"commit {name}")


def make_repo(name="repo"):
    root = Path(_TMP) / name
    remote = Path(_TMP) / f"{name}.git"
    run(_TMP, "init", "--bare", str(remote))
    run(_TMP, "init", str(root))
    run(root, "config", "user.email", "switchboard@example.test")
    run(root, "config", "user.name", "Switchboard Test")
    commit(root, "base.txt", "base\n", "base")
    run(root, "branch", "-M", "master")
    run(root, "remote", "add", "origin", str(remote))
    run(root, "push", "-u", "origin", "master")
    run(root, "checkout", "-b", "codex/SESSION-15-learn")
    run(root, "push", "-u", "origin", "codex/SESSION-15-learn")
    return root


try:
    store.init_db(P)

    task = store.create_task(
        {"workstream_id": "SESSION", "title": "calibration fixture"},
        actor="test", project=P)
    task_id = task["task_id"]
    repo = make_repo("learn")
    session = store.create_work_session({
        "task_id": task_id,
        "agent_id": AGENT,
        "repo_role": "canonical",
        "branch": "codex/SESSION-15-learn",
        "worktree_path": str(repo),
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "unknown",
        "policy_profile": "code_strict",
    }, actor="test", project=P)
    ws_id = session["work_session"]["work_session_id"]

    preflighted = store.preflight_work_session(
        ws_id, actor="test", project=P,
        expected_branch="codex/SESSION-15-learn")
    recorded = preflighted.get("preflight_run") or {}
    ok(recorded.get("recorded") is True and recorded.get("run", {}).get("run_id"),
       "preflight_work_session persists a preflight_run")
    run_id = recorded["run"]["run_id"]
    fetched = pf.get_preflight_run(run_id, project=P)
    ok(fetched and fetched["task_id"] == task_id and fetched["verdict"] == "pass",
       "get_preflight_run returns lasting prediction with findings list")
    ok(isinstance(fetched.get("findings"), list),
       "run carries findings array (may be empty on clean pass)")

    # Dirty worktree prediction
    (repo / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    dirty = store.preflight_work_session(
        ws_id, actor="test", project=P,
        expected_branch="codex/SESSION-15-learn")
    dirty_run = dirty.get("preflight_run") or {}
    ok(dirty_run.get("recorded") is True
       and dirty_run["run"]["verdict"] == "deny"
       and dirty_run["run"]["blocking_count"] >= 1,
       "dirty preflight records blocking finding prediction")
    dirty_fetched = pf.get_preflight_run(dirty_run["run"]["run_id"], project=P)
    ok(any(f.get("failure_class") == "dirty_worktree" for f in dirty_fetched["findings"]),
       "persisted findings include dirty_worktree with remediation")
    ok(any(f.get("remediation") for f in dirty_fetched["findings"]),
       "persisted findings keep remediation text")
    (repo / "scratch.txt").unlink()

    listed = pf.list_preflight_runs(task_id=task_id, project=P)
    ok(len(listed) >= 2, "list_preflight_runs returns history for task")

    # Seed three predictive outcomes for dirty_worktree calibration.
    for i in range(3):
        t = store.create_task(
            {"workstream_id": "SESSION", "title": f"cal-{i}"},
            actor="test", project=P)
        head = f"{i+1:040d}"
        pf.record_preflight_run({
            "task_id": t["task_id"],
            "agent_id": AGENT,
            "head_sha": head,
            "base_sha": head,
            "branch": "codex/SESSION-15-learn",
            "repo_role": "canonical",
            "verdict": "deny",
            "ok": False,
            "findings": [{
                "code": "dirty_worktree",
                "failure_class": "dirty_worktree",
                "severity": "high",
                "blocking": True,
                "message": "dirty",
                "remediation": "commit or stash",
            }],
        }, actor="test", source="test", project=P)
        # CI failure outcome on same head
        with store._conn(P) as c:
            c.execute(
                "INSERT INTO external_ci_runs("
                "run_id, source_project, source_repo, source_sha, mirror_repo, "
                "mirror_branch, workflow, status, conclusion, failure_class, "
                "task_id, requested_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"cirun-{i}", P, "6th-Element-Labs/projectplanner", head,
                    "6th-Element-Labs/projectplanner-ci", "main", "verify",
                    "completed", "failure", "failed_gate", t["task_id"],
                    time.time(), time.time(),
                ),
            )

    cal = pf.preflight_calibration(code="dirty_worktree", min_outcomes=3, project=P)
    ok(cal["schema"] == "switchboard.preflight_calibration.v1",
       "calibration uses typed schema")
    code_row = next((c for c in cal["codes"] if c["code"] == "dirty_worktree"), None)
    ok(code_row is not None and code_row["outcome_count"] >= 3,
       "dirty_worktree has enough outcomes for a recommendation")
    ok(code_row and code_row["ci_failures_after"] >= 3
       and code_row["recommendation"] == "keep_blocking",
       "predictive CI failures recommend keep_blocking")

    # Friction case: blocking finding but clean outcomes → consider_warn
    for i in range(3):
        t = store.create_task(
            {"workstream_id": "SESSION", "title": f"friction-{i}"},
            actor="test", project=P)
        head = f"a{i:039d}"
        pf.record_preflight_run({
            "task_id": t["task_id"],
            "agent_id": AGENT,
            "head_sha": head,
            "verdict": "deny",
            "ok": False,
            "findings": [{
                "code": "missing_upstream",
                "failure_class": "missing_upstream",
                "severity": "medium",
                "blocking": True,
                "message": "no upstream",
            }],
        }, actor="test", source="test", project=P)
        with store._conn(P) as c:
            c.execute(
                "INSERT INTO task_git_state(task_id, head_sha, merged_sha, merged_at, "
                "in_main_content, evidence_json, updated_at) VALUES (?,?,?,?,?,?,?)",
                (t["task_id"], head, head, time.time(), 1, "{}", time.time()),
            )
            c.execute(
                "INSERT INTO external_ci_runs("
                "run_id, source_project, source_repo, source_sha, mirror_repo, "
                "mirror_branch, workflow, status, conclusion, task_id, "
                "requested_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"cirun-ok-{i}", P, "6th-Element-Labs/projectplanner", head,
                    "6th-Element-Labs/projectplanner-ci", "main", "verify",
                    "completed", "success", t["task_id"], time.time(), time.time(),
                ),
            )

    friction = pf.preflight_calibration(code="missing_upstream", min_outcomes=3, project=P)
    frow = next((c for c in friction["codes"] if c["code"] == "missing_upstream"), None)
    ok(frow and frow["recommendation"] == "consider_warn",
       "blocking findings with clean outcomes recommend consider_warn")

    via_store = store.preflight_calibration(min_outcomes=3, project=P)
    ok(via_store.get("code_count", 0) >= 2,
       "store façade exports preflight_calibration")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
