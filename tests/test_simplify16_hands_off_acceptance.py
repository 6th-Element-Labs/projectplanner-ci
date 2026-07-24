"""SIMPLIFY-16: machine-check the recorded four-task hands-off proof."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import coordinator_daemon


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence/SIMPLIFY-16-hands-off-run.json"


def test_recorded_four_task_run_satisfies_hard_handoff_and_exact_head_gates():
    proof = json.loads(EVIDENCE.read_text())
    assert proof["evidence_mode"] == "hermetic_acceptance_fixture"
    tasks = proof["tasks"]
    assert len(tasks) == 4
    assert sorted(task["path"] for task in tasks) == [
        "clean", "clean", "red_ci", "review_correction",
    ]

    execution_ids: set[str] = set()
    for task in tasks:
        implementation = task["implementation"]
        assert implementation["role"] == "implementation"
        assert implementation["terminal_ack_sequence"] < implementation["in_review_sequence"]
        for signal in (
            "pid_terminal", "pty_terminal", "token_rejected", "lease_terminal",
            "fleet_live_absent", "direct_session_authority_terminal",
        ):
            assert implementation[signal] is True

        generations = [implementation, *task["role_generations"]]
        assert [row["generation"] for row in generations] == list(
            range(1, len(generations) + 1)
        )
        assert any(row["role"] == "review_merge" for row in task["role_generations"])
        assert all(row["execution_id"] not in execution_ids for row in generations)
        execution_ids.update(row["execution_id"] for row in generations)

        heads = task["heads"]
        assert heads["pr"] == heads["ci"] == heads["verdict"]
        assert len(heads["merge_group"]) == 40
        assert len(heads["canonical_merged"]) == 40
        assert task["done_provenance"] == "github_pr_merged"
        assert task["merge_queue_entries"] == 1

        if task["path"] in {"red_ci", "review_correction"}:
            assert task["remediation_rounds"] == 1
            assert task["prior_head"] != heads["pr"]
            assert any(row["role"] == "remediation" for row in task["role_generations"])


def test_restart_scope_messaging_and_zero_manual_action_proof():
    proof = json.loads(EVIDENCE.read_text())
    assert set(row["component"] for row in proof["restarts"]) == {
        "coordinator", "agent_host",
    }
    assert all(row.get("duplicate_dispatches", 0) == 0 for row in proof["restarts"])
    assert all(row.get("duplicate_merge_queue_entries", 0) == 0
               for row in proof["restarts"])
    assert all(value == 0 for value in proof["manual_actions"].values())

    scope = proof["scope_authority"]
    assert scope["max_concurrent_leases"] >= 2
    assert scope["canonical_registry"] == "autopilot_scopes"
    assert scope["exact_holder_fence_generation_enforced"] is True
    assert scope["out_of_scope_refusals"] >= 2
    assert scope["out_of_scope_side_effects"] == 0
    assert scope["race_safe_takeovers"] >= 1
    assert scope["closed_scope_authority_released"] is True
    assert scope["restart_resumed_only_durable_scopes"] is True

    messaging = proof["messaging"]
    assert set(messaging["cases"]) == {
        "ack_timeout", "duplicate_timeout", "late_ack",
        "stale_recipient_generation", "unavailable_target", "monitor_restart",
    }
    assert messaging["audit_facts"] == 1
    assert messaging["sender_notifications"] == 1
    assert messaging["operator_findings"] == 1
    for field in (
        "wake_effects", "restart_effects", "supersession_effects",
        "retry_effects", "lease_mutations", "process_stops",
        "nonterminal_monitors",
    ):
        assert messaging[field] == 0


def test_janitor_publishes_explicit_zero_messaging_census():
    census = coordinator_daemon.CoordinatorDaemon._drain_lifecycle(None, "switchboard")[
        "action_census"
    ]
    required_zero = {
        "start_task", "review", "remediation", "merge", "retry", "message",
        "send_agent_message", "work_instruction", "acknowledgement_monitor",
        "agent_directed_message",
    }
    assert required_zero <= census.keys()
    assert all(census[action] == 0 for action in required_zero)

    recorded = json.loads(EVIDENCE.read_text())["janitor_action_census"]
    assert all(recorded[action] == 0 for action in required_zero)
    source = inspect.getsource(coordinator_daemon.CoordinatorDaemon)
    assert "send_agent_message(" not in source
