#!/usr/bin/env python3
"""BUG-184 — the completion classifier and its durable store must share a vocabulary.

``classify_completion`` is the decision authority; ``completion_runs`` records what
it decided. The two enums were introduced two PRs apart — the store in #818
(SIMPLIFY-22), the classifier's ``assessing`` in #820 (SIMPLIFY-23) — and nothing
asserted they agree. ``assessing`` is the review branch (``review_required``,
``review_verdict_stale``, the review findings route), which is the most common
route in the system, so every review tick raised
``CompletionRunError: unsupported completion state: assessing`` at the persist
step in ``run_completion_tick`` — before it could plan an effect, fence a stale
runner, or dispatch a review generation.

It stayed hidden because COORD-49 (#878) is what routed traffic into it. Before
that fix a red ``Switchboard / merge authorization`` context was misrouted to
``required_exact_head_ci_failed`` → remediation → state ``blocked``, which IS
writable: the fleet burned remediation runners pointlessly but kept moving. The
correct fix moved those cases to the review branch and the fleet stopped.

The first two tests are the guard that makes this class of drift impossible; the
rest pin the live path that was broken.
"""
from __future__ import annotations

import ast
import pathlib
import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.domain.completion import state_machine
from switchboard.domain.completion.effects import plan_effect
from switchboard.domain.completion.executor import _completion_run_data
from switchboard.domain.mission_bot.outputs import MissionOutput
from switchboard.domain.mission_bot import adapter as mission_adapter
from switchboard.domain.mission_bot import reducer as mission_reducer
from switchboard.storage.migrations import runner as migrations
from switchboard.storage.repositories import completion_runs, task_completion


HEAD = "a" * 40
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/863"


def _mission_outputs(module) -> set[str]:
    """MissionOutput members referenced by one source module."""
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    values: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "MissionOutput"
            and node.attr in MissionOutput.__members__
        ):
            values.add(MissionOutput[node.attr].value)
    return values


class VocabularyConformanceTest(unittest.TestCase):
    """Reducer and executor must share the eight-output Mission Bot law."""

    def test_reducer_only_emits_declared_mission_outputs(self):
        emitted = _mission_outputs(mission_reducer)
        declared = {item.value for item in MissionOutput}
        self.assertTrue(emitted, "expected Mission Bot reducer outputs")
        self.assertEqual(emitted - declared, set())

    def test_executor_handles_every_declared_mission_output(self):
        handled = _mission_outputs(mission_adapter)
        declared = {item.value for item in MissionOutput}
        self.assertEqual(declared - handled, set())

    def test_old_classifier_state_is_not_mission_bot_authority(self):
        declared = {item.value for item in MissionOutput}
        self.assertNotIn("assessing", declared)
        self.assertIn("START_REVIEW", declared)

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
