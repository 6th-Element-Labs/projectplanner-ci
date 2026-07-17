#!/usr/bin/env python3
"""BUG-78: repaired semantic evidence replaces stale blocking markers."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="bug78-semantic-evidence-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
AGENT = "codex/BUG-78"
REPO = "6th-Element-Labs/projectplanner"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def completion_evidence(task_id, session, **semantic):
    result = {
        "task_id": task_id,
        "branch": session["branch"],
        "head_sha": session["head_sha"],
        "pr_url": f"https://github.com/{REPO}/pull/78",
        "pr_number": 78,
        "git_diff_check": "clean",
        "executed_test_run": {
            "schema": "switchboard.executed_test_run.v1",
            "run_id": f"run-{task_id}",
            "work_session_id": session["work_session_id"],
            "branch": session["branch"],
            "head_sha": session["head_sha"],
            "commands": ["python3 test_bug78_semantic_evidence_normalization.py"],
            "exit_code": 0,
            "status": "success",
            "completed_at": 1234.0,
            "output_hash": "sha256:" + "b" * 64,
        },
    }
    result.update(semantic)
    return result


try:
    store.init_project_registry()
    store.init_db(P)
    store.set_project_repo_topology(project=P, canonical_repo=REPO)
    store.register_agent(AGENT, "codex", lane="BUG", project=P)
    task = store.create_task(
        {"workstream_id": "BUG", "title": "repaired outcome supersedes stale blockers"},
        actor="test", project=P)
    branch = f"codex/{task['task_id']}-semantic-repair"
    head = "a" * 40
    session = {
        "task_id": task["task_id"], "agent_id": AGENT, "runtime": "codex",
        "repo_role": "canonical", "repo": REPO, "default_branch": "master",
        "branch": branch, "upstream": "origin/master", "base_sha": "base-ok",
        "head_sha": head, "worktree_path": f"/tmp/{task['task_id'].lower()}",
        "storage_mode": "worktree", "status": "active", "dirty_status": "clean",
        "conflict_marker_count": 0, "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "allow", "findings": []}},
    }
    claimed = store.claim_task(
        task["task_id"], AGENT, work_session=session, require_work_session=True,
        session_policy_profile="code_strict", actor="test", project=P)
    ok(claimed.get("claimed") is True, "claim starts with a bound Work Session")
    session["work_session_id"] = claimed["work_session_id"]

    no_go = completion_evidence(
        task["task_id"], session,
        verdict="nogo", process_cut_authorized=False,
        blocking_gate="G1_ports_independence",
        blocking_gates=["G1_ports_independence"],
        failed_gates=["G1_ports_independence"],
        go_only_task_blocked="ARCH-MS-105",
    )
    blocked = store.complete_claim(
        claimed["claim_id"], evidence=no_go, actor="test", project=P)
    ok(blocked.get("reason") == "semantic_completion_failed",
       "initial No-Go remains durably blocked")

    repaired = completion_evidence(
        task["task_id"], session,
        verdict="go", process_cut_authorized=True, failed_gates=[],
    )
    completed = store.complete_claim(
        claimed["claim_id"], evidence=repaired, actor="test", project=P)
    ok(completed.get("completed") is True and completed.get("status") == "In Review",
       "fresh Go completes without empty-string blocker workarounds")

    stored = store.get_task(task["task_id"], project=P)["git_state"]["evidence"]
    ok(stored.get("verdict") == "go" and stored.get("process_cut_authorized") is True,
       "active semantic fields reflect the repaired Go decision")
    ok("blocking_gate" not in stored and "blocking_gates" not in stored
       and "go_only_task_blocked" not in stored,
       "omitted stale blocking markers are removed from active evidence")
    history = stored.get("semantic_evidence_history") or {}
    previous = (history.get("superseded") or [{}])[-1]
    ok(previous.get("verdict") == "nogo"
       and previous.get("blocking_gate") == "G1_ports_independence",
       "superseded No-Go remains available as explicit history")

    merge_payload = {
        **repaired,
        "agent_id": AGENT,
        "claim_id": claimed["claim_id"],
        "work_session_id": session["work_session_id"],
        "repo": REPO,
        "target_branch": "master",
        "pr": {
            "number": 78,
            "html_url": f"https://github.com/{REPO}/pull/78",
            "draft": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "base": {"ref": "master"},
            "head": {"ref": branch, "sha": head},
        },
    }
    gate = store.merge_gate(merge_payload, actor="test", project=P)
    ok(not any(f.get("code") == "semantic_completion_failed"
               for f in gate.get("findings", [])),
       "merge gate evaluates only the current Go decision")

    with store._conn(P) as conn:
        store._upsert_git_state(conn, task["task_id"], {
            "evidence": {"source": "github-webhook", "merged_sha": "merge-78"},
        })
    after_provider = store.get_task(task["task_id"], project=P)["git_state"]["evidence"]
    ok(after_provider.get("verdict") == "go"
       and after_provider.get("semantic_evidence_history") == history,
       "non-semantic provider updates preserve current decision and history")

    print(f"\nBUG-78 semantic evidence normalization: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
