#!/usr/bin/env python3
"""Coordinator tick logging stays bounded (2026-07-30 journald churn incident).

The autopilot printed the full tick result — embedded dossier/snapshot JSON
included — every poll, churning the whole 222MB journald cap in ~2h and
destroying overnight forensics. The durable full record already lives in
decision_records/decision_episodes; scope state now keeps the same bounded
receipt as the journal: task_id, output, reason_code, and the effect receipt.
Full JSON stays available behind
PM_COORDINATOR_AUTOPILOT_LOG_FULL=1.
"""
from __future__ import annotations

import contextlib
import io
import json

from path_setup import ROOT  # noqa: F401
import coordinator_daemon


def _fat_completion_tick(task_id: str = "COORD-57") -> dict:
    return {
        "schema": "switchboard.completion_tick.v1",
        "controller": "mission_bot",
        "task_id": task_id,
        "snapshot": {"task_id": task_id, "blob": "s" * 400_000},
        "plan": {"output": "wait", "dossier": {"blob": "p" * 400_000}},
        "command": {"output": "wait", "dossier": {"blob": "c" * 400_000}},
        "decision": {
            "schema": "switchboard.completion_decision.v1",
            "state": "awaiting_ci",
            "route": "implementation",
            "reason_code": "ci_pending",
            "mission_output": "wait",
            "effect": "wait",
        },
        "observation": {
            "task_id": task_id,
            "output": "wait",
            "reason_code": "ci_pending",
            "role": "implementation",
            "head_sha": "abc1234",
            "shadow": {"blob": "o" * 100_000},
        },
        "execution": {
            "effect": "wait",
            "output": "wait",
            "run": {"blob": "r" * 100_000},
            "plan": {"dossier": {"blob": "e" * 100_000}},
            "command": {"dossier": {"blob": "e" * 100_000}},
            "result": {"action": "wait", "reason_code": "ci_pending"},
            "receipt": {
                "schema": "switchboard.mission_effect_receipt.v1",
                "effect": "wait",
                "output": "wait",
                "idem_key": "k-1",
                "verified": True,
                "pending": False,
                "idempotent_replay": False,
                "error": None,
            },
        },
        "decision_record": {"decision_id": "dec-1", "blob": "d" * 50_000},
    }


def _fat_tick_result() -> dict:
    wrapped = {
        "task_id": "COORD-57",
        "task_project": "switchboard",
        "status": "completion_tick",
        "completion": _fat_completion_tick("COORD-57"),
        "completion_wake": {"status": "not_supported", "task_id": "COORD-57"},
    }
    deliverable_scope = {
        "scope_id": "scope-deliv",
        "scope_type": "deliverable",
        "deliverable_id": "DEL-9",
        "status": "running",
        "task_id": None,
        "candidate_count": 1,
        "task_receipts": [wrapped],
        "error": None,
    }
    # Standalone task scopes store RAW completion ticks in task_receipts.
    task_scope = {
        "scope_id": "scope-task",
        "scope_type": "task",
        "deliverable_id": "",
        "status": "completion_tick",
        "task_id": "BUG-99",
        "candidate_count": 0,
        "task_receipts": [_fat_completion_tick("BUG-99")],
        "error": None,
    }
    failed_scope = {
        "scope_id": "scope-bad",
        "scope_type": "deliverable",
        "deliverable_id": "DEL-10",
        "status": "running",
        "task_id": None,
        "candidate_count": 1,
        "task_receipts": [{
            "task_id": "UI-1",
            "task_project": "switchboard",
            "status": "completion_tick_failed",
            "error": "RuntimeError",
            "reason": "boom " * 4000,
        }],
        "error": None,
    }
    project_result = {
        "schema": coordinator_daemon.RUN_SCHEMA,
        "project": "switchboard",
        "status": "running",
        "leader": True,
        "acting": True,
        "receipts": [deliverable_scope, task_scope, failed_scope],
        "lifecycle": {
            "status": "janitor_only",
            "decision_stream": [],
            "action_census": {"cleanup": 1, "start_task": 0},
        },
        "decision_stream": [],
        "completion_wakes": {
            "schema": "switchboard.completion_wake_drain.v1",
            "checked": 2,
            "accepted": 1,
            "failed": 0,
            "cancelled": 0,
            "results": [{"blob": "w" * 50_000}],
        },
        "state": {
            "sequence": 42,
            "status": "running",
            "last_result": deliverable_scope,
        },
    }
    return {
        "schema": coordinator_daemon.RUN_SCHEMA,
        "profile_id": "autopilot-default",
        "instance_id": "abc123",
        "projects": [project_result, {"project": "idle-board", "status": "standby",
                                      "leader": False, "lease": {"conflict": True}}],
        "ok": True,
    }


# 1. The summary keeps the routing facts and drops the megabytes.
fat = _fat_tick_result()
fat_line = json.dumps(fat, sort_keys=True, default=str)
assert len(fat_line) > 1_000_000

summary = coordinator_daemon.summarize_tick(fat)
line = json.dumps(summary, sort_keys=True, default=str)
assert len(line) < 4_000, f"summary still too large: {len(line)} bytes"
for leaked in ("snapshot", "dossier", "blob"):
    assert leaked not in line, f"summary leaked {leaked!r}"

assert summary["ok"] is True
assert summary["profile_id"] == "autopilot-default"
projects = {row["project"]: row for row in summary["projects"]}
assert projects["idle-board"]["status"] == "standby"
running = projects["switchboard"]
assert running["status"] == "running"
assert running["completion_wakes"] == {
    "checked": 2, "accepted": 1, "failed": 0, "cancelled": 0}

receipts = {row["scope_id"]: row for row in running["receipts"]}
wrapped_task = receipts["scope-deliv"]["tasks"][0]
assert wrapped_task["task_id"] == "COORD-57"
assert wrapped_task["output"] == "wait"
assert wrapped_task["reason_code"] == "ci_pending"
assert wrapped_task["effect"] == "wait"
assert wrapped_task["effect_verified"] is True

raw_task = receipts["scope-task"]["tasks"][0]
assert raw_task["task_id"] == "BUG-99"
assert raw_task["output"] == "wait"
assert raw_task["reason_code"] == "ci_pending"
assert raw_task["effect"] == "wait"

failed_task = receipts["scope-bad"]["tasks"][0]
assert failed_task["status"] == "completion_tick_failed"
assert failed_task["error"] == "RuntimeError"
assert len(failed_task["reason"]) <= 500

# 2. The env flag is parsed into the config.
default_config = coordinator_daemon.DaemonConfig.from_env({})
assert default_config.log_full_ticks is False
full_config = coordinator_daemon.DaemonConfig.from_env(
    {"PM_COORDINATOR_AUTOPILOT_LOG_FULL": "1"})
assert full_config.log_full_ticks is True

# 3. run_forever prints the bounded summary by default, full JSON on the flag.
class _Stub:
    pass


def _run_once(config: coordinator_daemon.DaemonConfig) -> str:
    daemon = coordinator_daemon.CoordinatorDaemon(config, store_mod=_Stub())

    def _sleep(_seconds):
        daemon._stop = True

    daemon.sleeper = _sleep
    daemon.tick = _fat_tick_result
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        daemon.run_forever()
    return out.getvalue()


summary_out = _run_once(coordinator_daemon.DaemonConfig())
assert len(summary_out) < 4_000, f"default tick line too large: {len(summary_out)}"
assert "blob" not in summary_out
assert "COORD-57" in summary_out

full_out = _run_once(coordinator_daemon.DaemonConfig(log_full_ticks=True))
assert "blob" in full_out
assert len(full_out) > 1_000_000

print("coordinator tick log volume: 3 passed")
