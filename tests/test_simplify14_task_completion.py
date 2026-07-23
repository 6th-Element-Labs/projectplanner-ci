import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.storage.migrations import runner as migrations
from switchboard.storage.repositories import task_completion


class TaskCompletionTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        for name, sql in migrations.DDL_MIGRATIONS:
            if name in {"0074_task_execution_completion_phases",
                        "0075_ix_task_execution_completion_identity"}:
                self.db.execute(sql)
        self.conn = patch.object(task_completion, "_conn", return_value=self.db)
        self.write = patch.object(
            task_completion, "_write_through", side_effect=lambda _project, fn: fn())
        self.conn.start()
        self.write.start()

    def tearDown(self):
        self.write.stop()
        self.conn.stop()
        self.db.close()

    def transition(self, phase="review_handoff", outcome="succeeded", **extra):
        data = {
            "task_id": "SIMPLIFY-14", "pr_number": 780,
            "head_sha": "a" * 40, "runner_generation": 3,
            "phase": phase, "outcome": outcome,
            "evidence": {"receipt_id": f"receipt-{phase}"},
        }
        data.update(extra)
        return task_completion.record_transition(data, actor="test", project="switchboard")

    def test_restart_recovery_and_idempotency(self):
        first = self.transition()
        again = self.transition()
        self.assertEqual(first["transition_id"], again["transition_id"])
        self.transition("ci")
        current = task_completion.get_completion(
            "SIMPLIFY-14", pr_number=780, head_sha="a" * 40,
            runner_generation=3, project="switchboard")
        self.assertEqual(current["phase"], "ci")
        self.assertEqual(len(current["transitions"]), 2)

    def test_identity_conflict_fails_closed(self):
        self.transition()
        with self.assertRaisesRegex(task_completion.TaskCompletionError, "identity conflict"):
            self.transition(evidence={"receipt_id": "different"})

    def test_failure_must_be_explicit(self):
        with self.assertRaisesRegex(task_completion.TaskCompletionError, "explicit failure"):
            self.transition("ci", outcome="failed", evidence={})
        row = self.transition(
            "ci", outcome="failed", evidence={},
            failure={"code": "exact_head_ci_failed", "check_run_id": 91})
        self.assertEqual(row["failure"]["code"], "exact_head_ci_failed")

    def test_head_and_generation_are_fenced(self):
        self.transition()
        self.transition(head_sha="b" * 40, runner_generation=4)
        old = task_completion.get_completion(
            "SIMPLIFY-14", pr_number=780, head_sha="a" * 40,
            runner_generation=3, project="switchboard")
        new = task_completion.get_completion(
            "SIMPLIFY-14", pr_number=780, head_sha="b" * 40,
            runner_generation=4, project="switchboard")
        self.assertEqual(old["head_sha"], "a" * 40)
        self.assertEqual(new["runner_generation"], 4)


if __name__ == "__main__":
    unittest.main()
