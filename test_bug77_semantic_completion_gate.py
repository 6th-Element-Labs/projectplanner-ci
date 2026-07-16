#!/usr/bin/env python3
"""BUG-77: merged provenance must not override a failed semantic outcome."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="bug77-semantic-completion-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
from switchboard.domain.provenance.semantic import semantic_completion_gate  # noqa: E402

P = "switchboard"
AGENT = "codex/BUG-77"
REPO = "6th-Element-Labs/projectplanner"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def make_task(title, description=""):
    return store.create_task(
        {"workstream_id": "BUG", "title": title, "description": description},
        actor="test", project=P)


def claim(task):
    branch = f"codex/{task['task_id']}-semantic-gate"
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
    ok(claimed.get("claimed") is True, f"{title(task)}: claim starts")
    session["work_session_id"] = claimed["work_session_id"]
    return claimed, session


def title(task):
    return task["title"]


def evidence(task, session, **extra):
    result = {
        "task_id": task["task_id"], "branch": session["branch"],
        "head_sha": session["head_sha"], "pr_url": f"https://github.com/{REPO}/pull/77",
        "pr_number": 77, "git_diff_check": "clean",
        "executed_test_run": {
            "schema": "switchboard.executed_test_run.v1", "run_id": f"run-{task['task_id']}",
            "work_session_id": session["work_session_id"], "branch": session["branch"],
            "head_sha": session["head_sha"], "commands": ["python3 test_bug77_semantic_completion_gate.py"],
            "exit_code": 0, "status": "success", "completed_at": 1234.0,
            "output_hash": "sha256:" + "b" * 64,
        },
    }
    result.update(extra)
    return result


try:
    store.init_project_registry()
    store.init_db(P)
    store.set_project_repo_topology(project=P, canonical_repo=REPO)
    store.register_agent(AGENT, "codex", lane="BUG", project=P)

    failed_task = make_task("failed semantic outcome stays open")
    failed_claim, failed_session = claim(failed_task)
    negative = evidence(
        failed_task, failed_session, verdict="nogo", process_cut_authorized=False,
        blocking_gate="G1_ports_independence", failed_gates=["G1_ports_independence"])
    completion = store.complete_claim(
        failed_claim["claim_id"], evidence=negative, actor="test", project=P)
    ok(completion.get("completed") is False
       and completion.get("reason") == "semantic_completion_failed",
       "complete_claim fails closed on explicit negative outcome evidence")
    with store._conn(P) as conn:
        claim_status = conn.execute(
            "SELECT status FROM task_claims WHERE id=?", (failed_claim["claim_id"],)
        ).fetchone()["status"]
    ok(claim_status == "active" and store.get_task(failed_task["task_id"], project=P)["status"] == "In Progress",
       "failed semantic completion keeps the same claim active for remediation")
    ok(store.get_task(failed_task["task_id"], project=P)["git_state"]["evidence"].get("verdict") == "nogo",
       "failed semantic evidence remains durable for merge/webhook backstops")

    merge_gate = store.merge_gate({
        **negative,
        "agent_id": AGENT,
        "claim_id": failed_claim["claim_id"],
        "work_session_id": failed_session["work_session_id"],
        "repo": REPO,
        "target_branch": "master",
        "pr": {
            "number": 77, "html_url": f"https://github.com/{REPO}/pull/77",
            "draft": False, "mergeable": True, "mergeable_state": "clean",
            "base": {"ref": "master"},
            "head": {"ref": failed_session["branch"], "sha": failed_session["head_sha"]},
        },
    }, actor="test", project=P)
    ok(any(f.get("code") == "semantic_completion_failed" for f in merge_gate.get("findings", [])),
       "merge_gate reports the same semantic failure")

    merged = store.mark_task_merged(
        failed_task["task_id"], "merge-sha-77", 77,
        f"https://github.com/{REPO}/pull/77", failed_session["branch"],
        failed_session["head_sha"], actor="github-webhook", project=P)
    failed_after_merge = store.get_task(failed_task["task_id"], project=P)
    ok(merged.get("status") == "Blocked" and failed_after_merge["status"] == "Blocked",
       "external merge records provenance but cannot stamp failed outcome Done")
    ok(failed_after_merge["git_state"]["merged_sha"] == "merge-sha-77",
       "semantic block preserves immutable merge provenance")

    decision_task = make_task(
        "explicit decision outcome may close",
        "semantic_completion_policy: decision\nA documented No-Go is an intentional terminal result.")
    decision_claim, decision_session = claim(decision_task)
    allowed_negative = evidence(
        decision_task, decision_session, verdict="nogo", process_cut_authorized=False,
        blocking_gate="G1_expected_no_go")
    allowed_completion = store.complete_claim(
        decision_claim["claim_id"], evidence=allowed_negative, actor="test", project=P)
    ok(allowed_completion.get("status") == "In Review",
       "task-owned decision policy explicitly permits terminal No-Go")
    allowed_merge = store.mark_task_merged(
        decision_task["task_id"], "merge-sha-decision", 78,
        f"https://github.com/{REPO}/pull/78", decision_session["branch"],
        decision_session["head_sha"], actor="github-webhook", project=P)
    ok(allowed_merge.get("status") == "Done",
       "authorized terminal No-Go may become Done after merge provenance")

    direct = semantic_completion_gate(
        {"task_id": "T-1", "description": "semantic_terminal_outcomes: nogo"},
        {"verdict": "nogo", "failed_gates": ["expected"]})
    ok(direct.get("ok") is True and direct.get("status") == "terminal_negative_outcome_authorized",
       "explicit terminal-outcome marker is deterministic and structured")

    structured = semantic_completion_gate(
        {"task_id": "T-2"},
        {"semantic_outcome": {"outcome": "blocked", "passed": False}})
    ok(structured.get("ok") is False
       and "semantic_outcome_not_passed" in structured.get("reasons", []),
       "structured semantic outcome fails closed without relying on legacy fields")

    print(f"\nBUG-77 semantic completion gate: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
