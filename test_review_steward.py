#!/usr/bin/env python3
"""Executable acceptance tests for the COORD-5 T2 review steward."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import review_steward as rs


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok  {message}")


NOW = 2_000_000.0


def make_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE tasks (
          task_id TEXT PRIMARY KEY, title TEXT, description TEXT,
          owner_person_or_role TEXT, assignee TEXT, phase TEXT, status TEXT,
          depends_on TEXT, entry_criteria TEXT, exit_criteria TEXT, risk_level TEXT,
          is_blocking INTEGER, sort_order INTEGER, updated_at REAL
        );
        CREATE TABLE task_git_state (
          task_id TEXT PRIMARY KEY, branch TEXT, head_sha TEXT, pushed_at REAL,
          pr_number INTEGER, pr_url TEXT, merged_sha TEXT, merged_at REAL,
          in_main_content INTEGER, published_ref TEXT, last_reconciled_at REAL,
          evidence_json TEXT, updated_at REAL
        );
        CREATE TABLE external_ci_runs (
          run_id TEXT PRIMARY KEY, source_sha TEXT, status_context TEXT, status TEXT,
          conclusion TEXT, run_url TEXT, failure_class TEXT, failure_reason TEXT,
          task_id TEXT, claim_id TEXT, agent_id TEXT, requested_at REAL,
          completed_at REAL, updated_at REAL
        );
        CREATE TABLE agent_presence (
          agent_id TEXT PRIMARY KEY, runtime TEXT, model TEXT, lane TEXT, task_id TEXT,
          control TEXT, principal_id TEXT, registered_at REAL, heartbeat_at REAL, ttl_s INTEGER
        );
        CREATE TABLE agent_hosts (
          host_id TEXT PRIMARY KEY, hostname TEXT, agent_host_version TEXT, repo_root TEXT,
          runtimes_json TEXT, limits_json TEXT, capacity_json TEXT, principal_id TEXT,
          registered_at REAL, heartbeat_at REAL, heartbeat_ttl_s INTEGER, status TEXT,
          last_error TEXT
        );
        CREATE TABLE task_claims (
          id TEXT PRIMARY KEY, task_id TEXT, agent_id TEXT, principal_id TEXT, status TEXT,
          claimed_at REAL, expires_at REAL, completed_at REAL, abandon_reason TEXT
        );
        CREATE TABLE file_leases (
          id TEXT PRIMARY KEY, agent_id TEXT, task_id TEXT, files TEXT,
          claimed_at REAL, ttl_minutes INTEGER, released_at REAL
        );
        CREATE TABLE resource_leases (
          id TEXT PRIMARY KEY, agent_id TEXT, principal_id TEXT, task_id TEXT,
          resource_type TEXT, names TEXT, claimed_at REAL, ttl_seconds INTEGER,
          released_at REAL
        );
        CREATE TABLE coordination_monitors (
          id TEXT PRIMARY KEY, kind TEXT, target_type TEXT, target_id TEXT, task_id TEXT,
          owner_agent TEXT, subject_agent TEXT, status TEXT, deadline REAL,
          condition_json TEXT, on_timeout_json TEXT, result_json TEXT,
          created_at REAL, updated_at REAL, last_checked_at REAL, fired_at REAL,
          resolved_at REAL
        );
        CREATE TABLE work_sessions (
          work_session_id TEXT PRIMARY KEY, task_id TEXT, claim_id TEXT, agent_id TEXT,
          runtime TEXT, repo_role TEXT, repo TEXT, default_branch TEXT, branch TEXT,
          upstream TEXT, base_sha TEXT, head_sha TEXT, storage_mode TEXT, status TEXT,
          dirty_status TEXT, conflict_marker_count INTEGER, hygiene_json TEXT,
          policy_profile TEXT, updated_at REAL, expires_at REAL
        );
        CREATE TABLE activity (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, actor TEXT, kind TEXT,
          payload TEXT, created_at REAL
        );
        CREATE TABLE decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT, title TEXT,
          context TEXT, decision TEXT, rationale TEXT, supersedes INTEGER, status TEXT,
          created_at REAL, decision_key TEXT, decision_kind TEXT, deliverable_id TEXT,
          coordinator_agent_id TEXT, inputs_json TEXT, policy_rule TEXT,
          chosen_action_json TEXT, skipped_alternatives_json TEXT, result_json TEXT
        );
        """)
        db.executemany(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("R-1", "Green review", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "Medium", 0, 1, NOW - 80),
                ("R-2", "Red review", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "Medium", 0, 2, NOW - 70),
                ("R-3", "Missing CI", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "Medium", 0, 3, NOW - 60),
                ("R-4", "Human gate", "human_gate required", "Named Reviewer", None, "P0",
                 "In Review", "[]", None, None, "High", 0, 4, NOW - 50),
                ("R-5", "Exhausted red", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "High", 1, 5, NOW - 40),
                ("R-6", "Not review", "", "Runtime", None, "P0", "Not Started",
                 "[]", None, None, "Low", 0, 6, NOW - 30),
            ],
        )
        db.executemany(
            "INSERT INTO task_git_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("R-1", "agent/r-1", "h1", NOW - 100, 11, "https://example/pr/11", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("R-2", "agent/r-2", "h2", NOW - 100, 12, "https://example/pr/12", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("R-3", "agent/r-3", "h3", NOW - 100, 13, "https://example/pr/13", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("R-4", "agent/r-4", "h4", NOW - 100, 14, "https://example/pr/14", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("R-5", "agent/r-5", "h5", NOW - 100, 15, "https://example/pr/15", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
            ],
        )
        db.executemany(
            "INSERT INTO external_ci_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("ci-1", "h1", "required", "completed", "success", "https://ci/1", None,
                 None, "R-1", None, "agent-a", NOW - 80, NOW - 20, NOW - 20),
                ("ci-2", "h2", "required", "completed", "failure", "https://ci/2",
                 "failed_gate", "tests failed", "R-2", None, "agent-a", NOW - 70,
                 NOW - 15, NOW - 15),
                ("ci-5a", "h5", "required", "completed", "failure", "https://ci/5a",
                 "failed_gate", "flake", "R-5", None, "agent-a", NOW - 50,
                 NOW - 40, NOW - 40),
                ("ci-5b", "h5", "required", "completed", "failure", "https://ci/5b",
                 "failed_gate", "still red", "R-5", None, "agent-a", NOW - 30,
                 NOW - 20, NOW - 20),
            ],
        )


def test_plan_actions():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "board.db"
        make_db(db_path)
        snapshot = __import__("coordinator_audit").collect_snapshot(
            str(db_path), "switchboard", now=NOW)
        plan = rs.plan_review_actions(snapshot, max_ci_reruns=2, now=NOW)
        by_task = {row["task_id"]: row for row in plan["actions"]}
        ok(by_task["R-1"]["action"] == rs.ACTION_DISPATCH_REVIEW,
           "green CI + clear deps dispatches review_merge")
        ok(by_task["R-1"]["merges"] is False, "green path never marks merges=True")
        ok(by_task["R-2"]["action"] == rs.ACTION_DISPATCH_REVIEW,
           "red CI still gets a verdict session, not an in-place repair")
        ok(by_task["R-3"]["action"] == rs.ACTION_DISPATCH_REVIEW,
           "missing CI is recorded by the exact-head verdict session")
        ok(by_task["R-4"]["action"] == rs.ACTION_DISPATCH_REVIEW,
           "legacy human-gate metadata does not suppress autonomous review")
        ok(by_task["R-5"]["action"] == rs.ACTION_DISPATCH_REVIEW,
           "persistently red CI does not trigger remediation in the reviewer")
        ok("R-6" not in by_task, "non In-Review tasks are ignored")
        ok(plan["summary"]["in_review_count"] == 5, "in_review_count excludes Not Started")


def test_dry_run_records_decisions_without_effects():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "board.db"
        make_db(db_path)
        activities = []
        decisions = []
        dispatches = []
        messages = []
        wakes = []

        def activity_writer(kind, actor, payload, project="switchboard"):
            activities.append((kind, actor, payload, project))
            return len(activities)

        def decision_writer(**kwargs):
            decisions.append(kwargs)
            return {
                "decision_id": f"coorddec-{len(decisions):04d}",
                "created": True,
                "schema": "switchboard.coordinator_decision.v1",
            }

        def scratchpad_dispatcher(pr_number, head_sha="", project="switchboard"):
            dispatches.append((pr_number, head_sha, project))
            return {"dispatched": True, "pr": pr_number, "head_sha": head_sha}

        def message_sender(**kwargs):
            messages.append(kwargs)
            return {"id": len(messages)}

        def wake_requester(**kwargs):
            wakes.append(kwargs)
            return {"wake_id": f"wake-{len(wakes)}", "requested": True}

        receipt = rs.steward_project(
            "switchboard",
            dry_run=True,
            persist=True,
            max_ci_reruns=2,
            now=NOW,
            db_path_resolver=lambda _project: str(db_path),
            activity_writer=activity_writer,
            decision_writer=decision_writer,
            scratchpad_dispatcher=scratchpad_dispatcher,
            message_sender=message_sender,
            wake_requester=wake_requester,
        )
        ok(receipt["ok"] is True, "dry-run steward succeeds")
        ok(receipt["dry_run"] is True, "receipt stamps dry_run")
        ok(receipt["effects"]["merged"] is False, "never merges")
        ok(not dispatches and not messages and not wakes,
           "dry-run performs no mutating side effects")
        ok(len(activities) == 1 and activities[0][0] == rs.ACTIVITY_KIND,
           "one review steward activity artifact")
        ok(len(decisions) >= 4, "structured decisions recorded for planned actions")
        ok(all(d["policy_rule"].startswith("coord.review.") for d in decisions),
           "policy_rule namespace is coord.review.*")
        ok(all("inputs_snapshot" in d and "skipped_alternatives" in d for d in decisions),
           "COORD-3 decision fields present")


def test_acting_mode_reruns_and_dispatches():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "board.db"
        make_db(db_path)
        dispatches = []
        starts = []
        task_messages = []
        control_waits = []

        def scratchpad_dispatcher(pr_number, head_sha="", project="switchboard"):
            dispatches.append({"pr": pr_number, "head_sha": head_sha, "project": project})
            return {"dispatched": True, "pr": pr_number, "head_sha": head_sha}

        def task_starter(task_id, **kwargs):
            starts.append({"task_id": task_id, **kwargs})
            return {"action": "attach" if task_id == "R-2" else "started",
                    "attached": task_id == "R-2", "started": task_id != "R-2",
                    "execution_id": f"run-{task_id.lower()}",
                    "role": kwargs.get("role")}

        def task_messenger(task_id, text, **kwargs):
            task_messages.append({"task_id": task_id, "text": text, **kwargs})
            return {"queued": True, "control_request_id": f"control-{task_id.lower()}"}

        def control_request_waiter(request_id, **kwargs):
            control_waits.append({"request_id": request_id, **kwargs})
            return {"request_id": request_id, "status": "completed"}

        receipt = rs.steward_project(
            "switchboard",
            dry_run=False,
            persist=False,
            max_ci_reruns=2,
            now=NOW,
            db_path_resolver=lambda _project: str(db_path),
            scratchpad_dispatcher=scratchpad_dispatcher,
            task_starter=task_starter,
            task_messenger=task_messenger,
            control_request_waiter=control_request_waiter,
        )
        actions = {row["task_id"]: row for row in receipt["executed"]}
        ok(not dispatches, "review stewardship leaves CI dispatch to its own lifecycle")
        ok(len(task_messages) == 1 and task_messages[0]["task_id"] == "R-2",
           "a live implementation runner receives the review instruction via control")
        ok(control_waits and control_waits[0]["request_id"] == "control-r-2",
           "the steward waits for the exact inject control request")
        ok(actions["R-2"]["result"]["status"] == "instruction_acknowledged",
           "a completed inject records delivery acknowledgement, not ensured review")
        ok(actions["R-2"]["result"]["review_completed"] is False
           and actions["R-2"]["result"]["awaiting_exact_head_verdict"] is True,
           "instruction acknowledgement still requires the exact-head verdict")
        ok(any(row["task_id"] == "R-5" and row["role"] == "review_merge"
               and "Do not repair red code" in row["instruction"] for row in starts),
           "red exact-head review explicitly forbids in-session remediation")
        ok(actions["R-1"]["result"]["status"] == "new_generation_started",
           "green CI starts a distinct exact-head reviewer generation")
        ok(any(row["task_id"] == "R-1" and row["role"] == "review_merge"
               and row["source_sha"] == "h1"
               and "head_sha: h1" in row["instruction"] for row in starts),
           "review binds the role-aware start to the exact PR head")
        ok(receipt["effects"]["merged"] is False, "acting mode still never merges")


def test_attached_review_timeout_supersedes_and_starts_exact_head_generation():
    action = {
        "action": rs.ACTION_DISPATCH_REVIEW,
        "task_id": "BUG-141",
        "policy_rule": rs.POLICY[rs.ACTION_DISPATCH_REVIEW],
        "inputs": {
            "pr_number": 141,
            "pr_url": "https://example/pr/141",
            "head_sha": "a" * 40,
            "ci_state": "green",
            "ci_run_id": "ci-141",
        },
    }
    superseded = []
    retries = []

    def starter(task_id, **kwargs):
        return {"action": "attach", "attached": True, "started": False,
                "execution_id": "run-implementation"}

    def messenger(task_id, text, **kwargs):
        return {"queued": True, "delivered": False,
                "control_request_id": "runnerreq-review-141"}

    def waiter(request_id, **kwargs):
        return {"request_id": request_id, "status": "timeout"}

    def superseder(request_id, **kwargs):
        superseded.append({"request_id": request_id, **kwargs})
        return {"request_id": request_id, "status": "cancelled",
                "result": {"superseded": True}}

    def retrier(task_id, **kwargs):
        retries.append({"task_id": task_id, **kwargs})
        if len(retries) == 1:
            return {"action": "superseding", "started": False,
                    "superseded_execution_id": "run-implementation"}
        return {"action": "started", "started": True,
                "execution_id": "run-review-141"}

    outcome = rs._execute_action(
        action, project="switchboard", actor="test", operator_agent="operator",
        dry_run=False, scratchpad_dispatcher=None, task_starter=starter,
        task_messenger=messenger, control_request_waiter=waiter,
        control_request_superseder=superseder, task_retrier=retrier,
        runner_terminal_waiter=lambda *args, **kwargs: {"terminal": True},
        review_ack_timeout_s=0, review_restart_timeout_s=0)
    result = outcome["result"]
    ok(result["status"] == "review_handoff_failed"
       and result.get("failure_reason") == "ack_timeout_after_retry",
       "an unacknowledged attach retries once and then fails loudly")
    ok(superseded and superseded[0]["request_id"] == "runnerreq-review-141",
       "the timed-out inject control request is explicitly superseded")
    ok(not retries, "ack timeout never replaces or kills the live runner")
    ok(any(effect["kind"] == "retry_review_instruction"
           for effect in result["effects"]),
       "the same-runner instruction is retried exactly once")

    retries.clear()
    raced = rs._execute_action(
        action, project="switchboard", actor="test", operator_agent="operator",
        dry_run=False, scratchpad_dispatcher=None, task_starter=starter,
        task_messenger=messenger, control_request_waiter=waiter,
        control_request_superseder=lambda *args, **kwargs: {
            "request_id": "runnerreq-review-141", "status": "completed"},
        task_retrier=retrier,
        runner_terminal_waiter=lambda *args, **kwargs: {"terminal": True},
        review_ack_timeout_s=0, review_restart_timeout_s=0)
    ok(raced["result"]["status"] == "instruction_acknowledged" and not retries,
       "a host acknowledgement that wins the cancel race avoids a needless restart")


def test_fail_closed_unavailable_db():
    receipt = rs.steward_project(
        "switchboard",
        dry_run=True,
        persist=False,
        now=NOW,
        db_path_resolver=lambda _project: "/tmp/does-not-exist-coord5.db",
    )
    ok(receipt["ok"] is False, "missing DB fails closed")
    actions = receipt["plan"]["actions"]
    ok(actions and actions[0]["action"] == rs.ACTION_HOLD_GATE,
       "unavailable read path fails closed as a mechanical hold")


if __name__ == "__main__":
    test_plan_actions()
    test_dry_run_records_decisions_without_effects()
    test_acting_mode_reruns_and_dispatches()
    test_attached_review_timeout_supersedes_and_starts_exact_head_generation()
    test_fail_closed_unavailable_db()
    print("ALL PASS")
