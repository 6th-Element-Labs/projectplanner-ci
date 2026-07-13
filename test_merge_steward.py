#!/usr/bin/env python3
"""Executable acceptance tests for the COORD-7 T3 merge steward."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import merge_steward as ms


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ok  {message}")


NOW = 2_000_100.0


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
                ("M-1", "Green low risk", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "Low", 0, 1, NOW - 80),
                ("M-2", "Red CI", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "Low", 0, 2, NOW - 70),
                ("M-3", "High risk green", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "High", 0, 3, NOW - 60),
                ("M-4", "Human gate", "human_gate required", "Named Reviewer", None, "P0",
                 "In Review", "[]", None, None, "Low", 0, 4, NOW - 50),
                ("M-5", "Missing PR", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "Low", 0, 5, NOW - 40),
                ("M-6", "Pending CI", "", "Runtime", "agent-a", "P0", "In Review",
                 "[]", None, None, "Medium", 0, 6, NOW - 30),
                ("M-7", "Not review", "", "Runtime", None, "P0", "Not Started",
                 "[]", None, None, "Low", 0, 7, NOW - 20),
            ],
        )
        db.executemany(
            "INSERT INTO task_git_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("M-1", "agent/m-1", "h1", NOW - 100, 21, "https://example/pr/21", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("M-2", "agent/m-2", "h2", NOW - 100, 22, "https://example/pr/22", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("M-3", "agent/m-3", "h3", NOW - 100, 23, "https://example/pr/23", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("M-4", "agent/m-4", "h4", NOW - 100, 24, "https://example/pr/24", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("M-6", "agent/m-6", "h6", NOW - 100, 26, "https://example/pr/26", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
            ],
        )
        db.executemany(
            "INSERT INTO external_ci_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("ci-1", "h1", "required", "completed", "success", "https://ci/1", None,
                 None, "M-1", None, "agent-a", NOW - 80, NOW - 20, NOW - 20),
                ("ci-2", "h2", "required", "completed", "failure", "https://ci/2",
                 "failed_gate", "tests failed", "M-2", None, "agent-a", NOW - 70,
                 NOW - 15, NOW - 15),
                ("ci-3", "h3", "required", "completed", "success", "https://ci/3", None,
                 None, "M-3", None, "agent-a", NOW - 60, NOW - 20, NOW - 20),
                ("ci-4", "h4", "required", "completed", "success", "https://ci/4", None,
                 None, "M-4", None, "agent-a", NOW - 50, NOW - 20, NOW - 20),
                ("ci-6", "h6", "required", "queued", None, "https://ci/6", None,
                 None, "M-6", None, "agent-a", NOW - 10, None, NOW - 10),
            ],
        )


def test_plan_fail_closed_and_arm():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "board.db"
        make_db(db_path)
        snapshot = __import__("coordinator_audit").collect_snapshot(
            str(db_path), "switchboard", now=NOW)
        # No authority / disabled → green M-1 escalates missing authority (fail closed)
        plan = ms.plan_merge_actions(snapshot, policy={
            "enabled": True, "authority_granted": False, "risk_ceiling": "Medium",
        }, now=NOW)
        by_task = {row["task_id"]: row for row in plan["actions"]}
        ok(by_task["M-2"]["action"] == ms.ACTION_ESCALATE
           and by_task["M-2"]["escalation_class"] == "red_ci_product_judgment",
           "red CI escalates")
        ok(by_task["M-3"]["action"] == ms.ACTION_ESCALATE
           and by_task["M-3"]["escalation_class"] == "policy_violation",
           "high risk above ceiling escalates")
        ok(by_task["M-4"]["action"] == ms.ACTION_ESCALATE
           and by_task["M-4"]["escalation_class"] == "human_gate_required",
           "human gate escalates")
        ok(by_task["M-5"]["action"] == ms.ACTION_ESCALATE
           and by_task["M-5"]["escalation_class"] == "missing_provenance",
           "missing PR escalates")
        ok(by_task["M-6"]["action"] == ms.ACTION_HOLD_PENDING,
           "pending CI holds without arming")
        ok(by_task["M-1"]["action"] == ms.ACTION_ESCALATE
           and by_task["M-1"]["escalation_class"] == "absent_permission",
           "missing authority fail-closed even when CI green")
        ok("M-7" not in by_task, "Not Started tasks are ignored")

        # Enabled + authority → M-1 arms
        plan2 = ms.plan_merge_actions(snapshot, policy={
            "enabled": True, "authority_granted": True, "risk_ceiling": "Medium",
            "max_in_flight": 3,
        }, now=NOW)
        by_task2 = {row["task_id"]: row for row in plan2["actions"]}
        ok(by_task2["M-1"]["action"] == ms.ACTION_ARM and by_task2["M-1"]["merges"] is True,
           "policy-enabled green low-risk PR is armable")
        ok(by_task2["M-1"]["policy_rule"] == "coord.merge.arm_auto_merge",
           "arm uses COORD-3 policy rule")

        # Saturated → hold backpressure
        plan3 = ms.plan_merge_actions(snapshot, policy={
            "enabled": True, "authority_granted": True, "risk_ceiling": "Medium",
        }, saturated=True, now=NOW)
        by_task3 = {row["task_id"]: row for row in plan3["actions"]}
        ok(by_task3["M-1"]["action"] == ms.ACTION_HOLD_BACKPRESSURE,
           "saturation holds arm")


def test_merge_gate_classifier():
    plan = ms.classify_merge_gate_result(
        {"ok": False, "status": "blocked",
         "findings": [{"code": "pr_not_mergeable", "failure_class": "stale_branch",
                       "detail": "conflicts"}]},
        project="switchboard", task_id="M-1",
    )
    ok(plan and plan["escalation_class"] == "stale_branch_conflict",
       "merge_gate conflicts map to stale_branch_conflict")
    ok(plan["recommended_choices"] and plan["minimum_decision"],
       "escalation includes choices and minimum decision")
    ok(ms.classify_merge_gate_result({"ok": True, "status": "passed"},
                                     project="switchboard") is None,
       "passed merge_gate does not escalate")


def test_dry_run_and_acting_hooks():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "board.db"
        make_db(db_path)
        decisions = []
        activities = []
        armed = []
        escalated = []
        reconciled = []

        def decision_writer(**kwargs):
            decisions.append(kwargs)
            return {"decision_id": f"coorddec-{len(decisions)}", "created": True}

        def activity_writer(kind, actor, payload, project="switchboard"):
            activities.append({"kind": kind, "actor": actor, "payload": payload,
                               "project": project})
            return len(activities)

        receipt = ms.steward_project(
            "switchboard",
            dry_run=True,
            persist=True,
            policy={"enabled": True, "authority_granted": True, "risk_ceiling": "Medium"},
            now=NOW,
            db_path_resolver=lambda _p: str(db_path),
            decision_writer=decision_writer,
            activity_writer=activity_writer,
        )
        ok(receipt["ok"] and receipt["dry_run"] is True, "dry-run steward ok")
        ok(receipt["effects"]["merged"] is False and receipt["effects"]["done_set"] is False,
           "dry-run never merges or sets Done")
        ok(any(a["kind"] == ms.ACTIVITY_KIND for a in activities),
           "activity artifact recorded")
        ok(decisions, "COORD-3 decisions recorded in dry-run")

        def arm_fn(**kwargs):
            armed.append(kwargs)
            return {"ok": True, "pr_number": kwargs.get("pr_number")}

        def escalate_fn(plan, *, actor, alert_to):
            escalated.append({"plan": plan, "actor": actor, "alert_to": alert_to})
            return {"ok": True, "delivered": True, "message_id": 1}

        def reconcile_fn(**kwargs):
            reconciled.append(kwargs)
            return {"ok": True, "findings": []}

        act = ms.steward_project(
            "switchboard",
            dry_run=False,
            persist=True,
            policy={"enabled": True, "authority_granted": True, "risk_ceiling": "Medium",
                    "post_merge_reconcile": True},
            now=NOW,
            db_path_resolver=lambda _p: str(db_path),
            decision_writer=decision_writer,
            activity_writer=activity_writer,
            arm_fn=arm_fn,
            escalate_fn=escalate_fn,
            reconcile_fn=reconcile_fn,
        )
        ok(act["effects"]["merged"] is True, "acting arms auto-merge for eligible PR")
        ok(act["effects"]["done_set"] is False, "acting never sets Done")
        ok(any(row.get("task_id") == "M-1" for row in armed), "M-1 was armed")
        ok(reconciled, "post-arm reconcile requested")
        ok(any((e["plan"] or {}).get("escalation_class") == "red_ci_product_judgment"
               for e in escalated),
           "red CI escalates via COORD-6 delivery hook")


def test_policy_disabled_holds():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "board.db"
        make_db(db_path)
        snapshot = __import__("coordinator_audit").collect_snapshot(
            str(db_path), "switchboard", now=NOW)
        plan = ms.plan_merge_actions(snapshot, policy={
            "enabled": False, "authority_granted": True, "risk_ceiling": "Medium",
        }, now=NOW)
        by_task = {row["task_id"]: row for row in plan["actions"]}
        ok(by_task["M-1"]["action"] == ms.ACTION_HOLD_POLICY,
           "disabled policy observes green PR without arming")


if __name__ == "__main__":
    test_plan_fail_closed_and_arm()
    test_merge_gate_classifier()
    test_dry_run_and_acting_hooks()
    test_policy_disabled_holds()
    print("\nAll COORD-7 merge steward tests passed.")
