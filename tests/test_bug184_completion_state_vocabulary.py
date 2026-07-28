#!/usr/bin/env python3
"""BUG-184 — Mission Bot outputs and the durable store must share a vocabulary.

Mission Bot is the decision authority; ``completion_runs`` records what it
decided. Every state/route ``reduce_mission`` / ``classify_completion`` can emit
must be accepted by the store, or ticks die at persist before planning an effect.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.domain.completion import state_machine
from switchboard.domain.completion.effects import plan_effect
from switchboard.domain.completion.executor import _completion_run_data
from switchboard.domain.mission_bot import reduce_mission
from switchboard.domain.mission_bot.outputs import MissionOutput
from switchboard.storage.migrations import runner as migrations
from switchboard.storage.repositories import completion_runs, task_completion


HEAD = "a" * 40
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/863"


def _agent_blocker():
    return {
        "route": "agent_requires_human",
        "reason": "missing_credentials",
        "source_tool": "agent_requires_human",
        "binding": "registered_agent",
        "provenance_stamp": "switchboard.resolve_write_actor.v1",
        "agent_id": "agent-vocab-1",
        "actor": "agent-vocab-1",
        "execution_id": "exec-1",
        "execution_generation": 1,
    }


def _mission_fixture_snapshots() -> list[dict]:
    """Cover every Mission Bot output with a reasonable exact-head fixture."""
    base_pr = {
        "number": 863,
        "state": "OPEN",
        "draft": False,
        "mergeable": True,
        "mergeStateStatus": "CLEAN",
        "head": {"sha": HEAD},
        "url": PR_URL,
    }
    green_ci = [{"name": "Switchboard CI / VM gate", "conclusion": "success"}]
    red_ci = [{
        "name": "Switchboard CI / VM gate",
        "conclusion": "failure",
        "failure_attribution": "product",
    }]
    review_pass = {"status": "passed", "head_sha": HEAD, "pr_url": PR_URL}
    return [
        # START_IMPLEMENTATION
        state_machine.build_completion_snapshot(
            task={"task_id": "CO-IMPL", "status": "In Progress",
                  "git_state": {"head_sha": HEAD}},
            github_pr={},
        ),
        # START_REMEDIATION
        state_machine.build_completion_snapshot(
            task={"task_id": "CO-REM", "status": "In Review",
                  "git_state": {"head_sha": HEAD, "pr_number": 863,
                                "pr_url": PR_URL}},
            github_pr={**base_pr, "mergeStateStatus": "BLOCKED"},
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=red_ci,
            review=review_pass,
        ),
        # START_REVIEW / assessing
        state_machine.build_completion_snapshot(
            task={"task_id": "CO-REV", "status": "In Review",
                  "git_state": {"head_sha": HEAD, "pr_number": 863,
                                "pr_url": PR_URL}},
            github_pr=base_pr,
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=green_ci,
            review={},
        ),
        # MARK_READY
        state_machine.build_completion_snapshot(
            task={"task_id": "CO-DRAFT", "status": "In Review",
                  "git_state": {"head_sha": HEAD, "pr_number": 863,
                                "pr_url": PR_URL}},
            github_pr={**base_pr, "draft": True},
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=green_ci,
            review=review_pass,
        ),
        # ARM_MERGE / ready_to_queue
        state_machine.build_completion_snapshot(
            task={"task_id": "CO-ARM", "status": "In Review",
                  "git_state": {"head_sha": HEAD, "pr_number": 863,
                                "pr_url": PR_URL}},
            github_pr=base_pr,
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=green_ci,
            review=review_pass,
        ),
        # WAIT (live runner)
        state_machine.build_completion_snapshot(
            task={"task_id": "CO-WAIT", "status": "In Review",
                  "git_state": {"head_sha": HEAD, "pr_number": 863,
                                "pr_url": PR_URL}},
            github_pr=base_pr,
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=green_ci,
            review=review_pass,
            runner={"live": True, "role": "remediation"},
        ),
        # AGENT_REQUIRES_HUMAN
        {
            **state_machine.build_completion_snapshot(
                task={"task_id": "CO-HUMAN", "status": "In Review",
                      "git_state": {"head_sha": HEAD, "pr_number": 863,
                                    "pr_url": PR_URL}},
                github_pr={**base_pr, "mergeStateStatus": "BLOCKED"},
                required_status_contexts=["Switchboard CI / VM gate"],
                status_contexts=red_ci,
                review=review_pass,
            ),
            "work_session": {
                "status": "blocked",
                "hygiene": {"blocker": _agent_blocker()},
            },
        },
        # OBSERVE_MERGED / reconciling
        state_machine.build_completion_snapshot(
            task={"task_id": "CO-DONE", "status": "In Review",
                  "git_state": {"head_sha": HEAD, "pr_number": 863,
                                "pr_url": PR_URL}},
            github_pr={**base_pr, "state": "MERGED", "merged": True},
            merge_provenance={"merged_sha": HEAD},
        ),
    ]


def _emitted_from_mission_bot() -> tuple[set[str], set[str]]:
    """States/routes Mission Bot actually emits across a fixture set."""
    states: set[str] = set()
    routes: set[str] = set()
    seen_outputs: set[str] = set()
    for snap in _mission_fixture_snapshots():
        command = reduce_mission(snap)
        decision = state_machine.classify_completion({}, snap)
        states.add(str(command.get("state") or ""))
        routes.add(str(command.get("route") or ""))
        states.add(str(decision.get("state") or ""))
        routes.add(str(decision.get("route") or ""))
        seen_outputs.add(str(command.get("output") or ""))
    states.discard("")
    routes.discard("")
    # Fixture set must exercise the full eight-output Mission Bot surface.
    expected = {output.value for output in MissionOutput}
    missing = sorted(expected - seen_outputs)
    if missing:
        raise AssertionError(
            f"fixture set missed Mission Bot outputs: {missing}"
        )
    return states, routes


class VocabularyConformanceTest(unittest.TestCase):
    """The guard. These two assertions are the point of this file."""

    def test_every_state_the_classifier_emits_is_accepted_by_the_store(self):
        emitted, _routes = _emitted_from_mission_bot()
        self.assertTrue(emitted, "expected Mission Bot to emit states")
        self.assertEqual(
            sorted(emitted - completion_runs.STATES), [],
            "Mission Bot emits a state completion_runs rejects; every "
            "tick reaching it dies at the persist step before planning an effect",
        )

    def test_every_route_the_classifier_emits_is_accepted_by_the_store(self):
        _states, emitted = _emitted_from_mission_bot()
        self.assertTrue(emitted, "expected Mission Bot to emit routes")
        self.assertEqual(
            sorted(emitted - completion_runs.ROUTES), [],
            "Mission Bot emits a route completion_runs rejects",
        )

    def test_assessing_is_the_regressed_value(self):
        # Pins the specific defect: without the fix this set is {'assessing'}.
        emitted, _routes = _emitted_from_mission_bot()
        self.assertIn("assessing", emitted)
        self.assertIn("assessing", completion_runs.STATES)

    def test_assessing_is_not_terminal(self):
        # A task awaiting review is still in flight; it must keep being ticked.
        self.assertNotIn("assessing", completion_runs.TERMINAL_STATES)


class ReviewTickPersistsTest(unittest.TestCase):
    """The live path: a review-required decision must reach the store."""

    project = "switchboard"

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "assignee TEXT, updated_at REAL)")
        for name, sql in migrations.DDL_MIGRATIONS:
            if name in {
                "0074_task_execution_completion_phases",
                "0075_ix_task_execution_completion_identity",
                "0111_completion_runs",
                "0112_ux_completion_runs_task",
            }:
                self.db.execute(sql)
        self.db.execute(
            "INSERT INTO tasks(task_id, status, assignee, updated_at) "
            "VALUES ('CO-20','In Review',NULL,1.0)")
        self.db.commit()
        self.patches = [
            patch.object(completion_runs, "_conn", return_value=self.db),
            patch.object(completion_runs, "_write_through",
                         side_effect=lambda _project, fn: fn()),
            patch.object(task_completion, "_conn", return_value=self.db),
            patch.object(task_completion, "_write_through",
                         side_effect=lambda _project, fn: fn()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()

    def _review_required_snapshot(self):
        """A healthy PR whose only gap is a review verdict for the exact head.

        This is the shape CO-20, BUG-179 and BUG-183 were all in on prod.
        """
        return state_machine.build_completion_snapshot(
            task={"task_id": "CO-20", "status": "In Review",
                  "git_state": {"head_sha": HEAD, "pr_number": 863,
                                "pr_url": PR_URL}},
            github_pr={"number": 863, "state": "OPEN", "draft": False,
                       "mergeable": True, "mergeStateStatus": "CLEAN",
                       "head": {"sha": HEAD}, "url": PR_URL},
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=[{"name": "Switchboard CI / VM gate",
                              "conclusion": "success"}],
            review={},  # no verdict recorded for this head
            merge_gate={"findings": [], "pr_url": PR_URL},
        )

    def test_a_review_required_decision_persists(self):
        snapshot = self._review_required_snapshot()
        decision = state_machine.classify_completion({}, snapshot)
        self.assertEqual(decision["reason_code"], "review_required")
        self.assertEqual(decision["state"], "assessing")

        plan = plan_effect(decision, snapshot, {})
        row = _completion_run_data(decision, snapshot, plan)
        # Before BUG-184 this raised CompletionRunError and the tick died here,
        # so no effect was ever planned and no runner was ever dispatched.
        persisted = completion_runs.transition_completion_run_in(
            self.db, row, actor="test")
        self.assertEqual(persisted["state"], "assessing")
        self.assertEqual(persisted["route"], "review_merge")
        self.assertEqual(persisted["reason_code"], "review_required")

    def test_the_tick_can_reach_its_effect_once_the_decision_persists(self):
        snapshot = self._review_required_snapshot()
        decision = state_machine.classify_completion({}, snapshot)
        plan = plan_effect(decision, snapshot, {})
        # The whole point of persisting: the effect that produces a review.
        self.assertEqual(plan["effect"], "ensure_review_generation")
        self.assertEqual(plan["role"], "review_merge")

    def test_repeated_review_ticks_stay_idempotent(self):
        snapshot = self._review_required_snapshot()
        decision = state_machine.classify_completion({}, snapshot)
        plan = plan_effect(decision, snapshot, {})
        row = _completion_run_data(decision, snapshot, plan)
        first = completion_runs.transition_completion_run_in(
            self.db, row, actor="test")
        second = completion_runs.transition_completion_run_in(
            self.db, row, actor="test")
        self.assertEqual(first["state_version"], second["state_version"])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) AS n FROM completion_runs").fetchone()["n"], 1)

    def test_an_unknown_state_is_still_refused(self):
        # The guard stays fail-closed: widening the enum must not disable it.
        snapshot = self._review_required_snapshot()
        decision = dict(state_machine.classify_completion({}, snapshot))
        decision["state"] = "vibing"
        plan = plan_effect(decision, snapshot, {})
        row = _completion_run_data(decision, snapshot, plan)
        with self.assertRaises(completion_runs.CompletionRunError):
            completion_runs.transition_completion_run_in(
                self.db, row, actor="test")


if __name__ == "__main__":
    unittest.main()
