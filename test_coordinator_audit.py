#!/usr/bin/env python3
"""Executable acceptance tests for the COORD-2 T0 audit loop."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

import coordinator_audit as ca


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
        """)
        db.executemany(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("A-1", "Dependency", "", "Runtime", None, "P0", "Done", "[]",
                 None, None, "Low", 0, 1, NOW - 100),
                ("A-2", "Ready blocker", "", "Runtime", None, "P0", "Not Started",
                 '["A-1"]', None, None, "High", 1, 2, NOW - 90),
                ("A-3", "Green review", "", "Runtime", "agent-live", "P0", "In Review",
                 "[]", None, None, "Medium", 0, 3, NOW - 80),
                ("A-4", "Red review", "", "Runtime", "agent-live", "P0", "In Review",
                 "[]", None, None, "Medium", 0, 4, NOW - 70),
                ("A-5", "Unproven done", "", "Runtime", None, "P0", "Done",
                 "[]", None, None, "Medium", 0, 5, NOW - 60),
                ("A-6", "Approval", "human_gate required", "Named Reviewer", None, "P0",
                 "Not Started", "[]", None, None, "High", 0, 6, NOW - 50),
                ("A-7", "Expired work", "", "Runtime", "agent-stale", "P0", "In Progress",
                 "[]", None, None, "Medium", 0, 7, NOW - 40),
            ],
        )
        db.executemany(
            "INSERT INTO task_git_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("A-1", "agent/a-1", "h1", NOW - 200, 1, "https://example/pr/1", "m1",
                 NOW - 100, 1, None, NOW - 10, "{}", NOW - 10),
                ("A-3", "agent/a-3", "h3", NOW - 100, 3, "https://example/pr/3", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
                ("A-4", "agent/a-4", "h4", NOW - 100, 4, "https://example/pr/4", None,
                 None, 0, None, NOW - 10, "{}", NOW - 10),
            ],
        )
        db.executemany(
            "INSERT INTO external_ci_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("ci-3", "h3", "required", "completed", "success", "https://ci/3", None,
                 None, "A-3", None, "agent-live", NOW - 80, NOW - 20, NOW - 20),
                ("ci-4", "h4", "required", "completed", "failure", "https://ci/4",
                 "failed_gate", "test failed", "A-4", None, "agent-live", NOW - 70,
                 NOW - 15, NOW - 15),
            ],
        )
        db.executemany(
            "INSERT INTO agent_presence VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("agent-live", "codex", "gpt", "COORD", "A-3", "{}", "p-live",
                 NOW - 1000, NOW - 10, 120),
                ("agent-stale", "codex", "gpt", "COORD", "A-7", "{}", "p-stale",
                 NOW - 1000, NOW - 500, 120),
            ],
        )
        db.execute(
            "INSERT INTO agent_hosts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("host-1", "host", "v1", "/repos", '["codex"]', "{}", "{}", "p-host",
             NOW - 1000, NOW - 10, 60, "online", None),
        )
        db.execute(
            "INSERT INTO task_claims VALUES (?,?,?,?,?,?,?,?,?)",
            ("claim-old", "A-7", "agent-stale", "p-stale", "active", NOW - 500,
             NOW - 100, None, None),
        )
        db.execute(
            "INSERT INTO file_leases VALUES (?,?,?,?,?,?,?)",
            ("file-old", "agent-stale", "A-7", '["a.py"]', NOW - 5000, 30, None),
        )
        db.execute(
            "INSERT INTO resource_leases VALUES (?,?,?,?,?,?,?,?,?)",
            ("resource-old", "agent-stale", "p-stale", "A-7", "task", '["A-7"]',
             NOW - 5000, 1800, None),
        )
        db.execute(
            "INSERT INTO coordination_monitors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("mon-fired", "ack", "agent", "agent-stale", "A-7", "coord", "agent-stale",
             "fired", NOW - 100, "{}", "{}", "{}", NOW - 500, NOW - 50,
             NOW - 50, NOW - 50, None),
        )
        db.execute(
            "INSERT INTO work_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ws-unsafe", "A-4", None, "agent-live", "codex", "canonical", "org/repo",
             "master", "agent/a-4", "origin/master", "b", "h4", "worktree", "active",
             "dirty", 1, '{"deny":["conflict_markers"]}', "code_strict", NOW - 20,
             NOW + 1000),
        )
        db.execute("INSERT INTO meta VALUES (?,?)", ("canonical_main_sha", json.dumps("main")))
        db.execute("INSERT INTO meta VALUES (?,?)", ("github_repo", json.dumps("org/repo")))
        db.execute(
            "INSERT INTO activity(task_id,actor,kind,payload,created_at) VALUES (?,?,?,?,?)",
            (None, "reconcile", "reconcile.completed", "{}", NOW - 10),
        )


with tempfile.TemporaryDirectory() as temp:
    db_path = Path(temp) / "board.db"
    make_db(db_path)
    before_bytes = db_path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = db_path.stat().st_mtime_ns
    with sqlite3.connect(db_path) as db:
        before_activity = db.execute("SELECT COUNT(*) FROM activity").fetchone()[0]

    snapshot = ca.collect_snapshot(str(db_path), "switchboard", now=NOW)
    ok(snapshot["read_status"]["available"], "snapshot opens the board read-only")
    ok(snapshot["read_status"]["mode"] == "sqlite_mode_ro_query_only",
       "snapshot reports its enforced read mode")
    ok(len(snapshot["tasks"]) == 7 and len(snapshot["hosts"]) == 1,
       "snapshot inspects board tasks and host state")
    ok(len(snapshot["claims"]) == 1 and len(snapshot["monitors"]) == 1,
       "snapshot inspects claims and coordination monitors")

    after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with sqlite3.connect(db_path) as db:
        after_activity = db.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
    ok(before_hash == after_hash and before_mtime == db_path.stat().st_mtime_ns,
       "pure collection does not modify the database bytes or mtime")
    ok(before_activity == after_activity, "pure collection appends no activity")

    plan = ca.build_plan(snapshot, max_recommendations=100)
    again = ca.build_plan(snapshot, max_recommendations=100)
    ok(plan["decision_digest"] == again["decision_digest"] and
       plan["recommendations"] == again["recommendations"],
       "the same snapshot produces the same ranked plan")
    print(f"  queue counts: {plan['summary']['queue_counts']}")
    ok(all(plan["queues"][category] for category in ca.CATEGORIES),
       "the fixture produces assignment/review/merge/reconcile/stale-claim/escalation queues")
    ok(plan["effects"]["work_state_executed"] == [] and
       all(not row["mutates"] for row in plan["recommendations"]),
       "the plan records recommendations without executing work-state effects")
    merge_rows = [row for row in plan["recommendations"] if row["category"] == "merge"]
    ok(merge_rows[0]["action"] == "evaluate_safe_merge_gate" and
       merge_rows[0]["evidence"]["provider_truth"] == "not_read_live_by_t0_audit",
       "green board evidence recommends a safe-merge evaluation without claiming live truth")

    calls = []

    def writer(kind, actor, payload, *, project):
        calls.append((kind, actor, payload, project))
        return 77

    run = ca.audit_projects(
        ["switchboard"], persist=True, now=NOW,
        db_path_resolver=lambda _project: str(db_path), activity_writer=writer,
    )
    ok(run["ok"] and run["effects"]["work_state_executed"] == [],
       "scheduled wrapper succeeds without work-state mutations")
    ok(len(calls) == 1 and calls[0][0] == ca.ACTIVITY_KIND and calls[0][3] == "switchboard",
       "scheduled wrapper performs exactly one allowed plan-log write")
    ok(calls[0][2]["work_state_effects"] == [] and calls[0][2]["tier"] == "T0",
       "the audit artifact records T0 and an empty effect set")

missing = ca.audit_projects(
    ["missing"], persist=False, now=NOW,
    db_path_resolver=lambda _project: "/does/not/exist.db",
)
missing_plan = missing["projects"][0]["plan"]
ok(not missing["ok"], "a missing database fails the run closed")
ok(missing_plan["recommendations"][0]["action"] == "restore_audit_read_path" and
   missing_plan["recommendations"][0]["priority"] == "critical",
   "a missing database becomes a critical escalation, not an empty green plan")

root = Path(__file__).parent
service = (root / "deploy" / "projectplanner-coordinator-audit.service").read_text()
timer = (root / "deploy" / "projectplanner-coordinator-audit.timer").read_text()
ok("jobs.py coordinator_audit" in service and "RestrictAddressFamilies=AF_UNIX" in service,
   "systemd service runs the audit without network access")
ok("OnUnitActiveSec=5min" in timer and "Persistent=true" in timer,
   "systemd timer schedules a persistent five-minute audit")

print("\nAll coordinator_audit tests passed.")
