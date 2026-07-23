"""SIMPLIFY-22 — completion_runs as durable current-state authority."""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.storage.migrations import runner as migrations
from switchboard.storage.repositories import completion_runs
from switchboard.storage.repositories import task_completion


class CompletionRunsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE tasks ("
            "task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "assignee TEXT, updated_at REAL)")
        self.db.execute(
            "CREATE TABLE task_git_state ("
            "task_id TEXT PRIMARY KEY, pr_number INTEGER, head_sha TEXT, "
            "branch TEXT, pr_url TEXT, merged_sha TEXT, evidence_json TEXT)")
        for name, sql in migrations.DDL_MIGRATIONS:
            if name in {
                "0074_task_execution_completion_phases",
                "0075_ix_task_execution_completion_identity",
                "0111_completion_runs",
                "0112_ux_completion_runs_task",
            }:
                self.db.execute(sql)
        self.patches = [
            patch.object(completion_runs, "_conn", return_value=self.db),
            patch.object(
                completion_runs, "_write_through",
                side_effect=lambda _project, fn: fn()),
            patch.object(task_completion, "_conn", return_value=self.db),
            patch.object(
                task_completion, "_write_through",
                side_effect=lambda _project, fn: fn()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()

    def _seed_task(self, task_id="SIMPLIFY-22", status="In Review",
                   pr_number=812, head_sha="a" * 40):
        self.db.execute(
            "INSERT OR REPLACE INTO tasks(task_id, status, assignee, updated_at) "
            "VALUES (?,?,?,?)",
            (task_id, status, None, 1.0))
        self.db.execute(
            "INSERT OR REPLACE INTO task_git_state("
            "task_id, pr_number, head_sha, branch, pr_url, merged_sha, evidence_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (task_id, pr_number, head_sha, f"codex/{task_id}-x",
             f"https://github.com/6th-Element-Labs/projectplanner/pull/{pr_number}",
             None, "{}"))
        self.db.commit()

    def _decision(self, **extra):
        data = {
            "task_id": "SIMPLIFY-22",
            "pr_number": 812,
            "head_sha": "a" * 40,
            "state": "waiting",
            "route": "wait",
            "reason_code": "ci_pending",
            "desired_role": "",
            "board_status": "In Review",
            "evidence_refs": {"ci": {"head_sha": "a" * 40, "status": "pending"}},
            "runner_generation": 3,
        }
        data.update(extra)
        return data

    def test_one_active_run_per_task(self):
        self._seed_task()
        first = completion_runs.transition_completion_run(
            self._decision(), actor="test", project="switchboard")
        again = completion_runs.transition_completion_run(
            self._decision(state="blocked", route="remediation",
                           reason_code="ci_failed", desired_role="remediation",
                           board_status="Blocked",
                           evidence_refs={"ci": {"head_sha": "a" * 40,
                                                 "status": "failed"}}),
            actor="test", project="switchboard")
        self.assertEqual(first["run_id"], again["run_id"])
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM completion_runs WHERE task_id=?",
            ("SIMPLIFY-22",)).fetchone()["n"]
        self.assertEqual(rows, 1)
        current = completion_runs.get_active_completion_run(
            "SIMPLIFY-22", project="switchboard")
        self.assertEqual(current["route"], "remediation")
        self.assertEqual(current["state"], "blocked")

    def test_same_state_replay_is_idempotent(self):
        self._seed_task()
        first = completion_runs.transition_completion_run(
            self._decision(), actor="test", project="switchboard")
        again = completion_runs.transition_completion_run(
            self._decision(), actor="test", project="switchboard")
        self.assertEqual(first["state_version"], again["state_version"])
        self.assertEqual(first["updated_at"], again["updated_at"])
        self.assertEqual(first["attempt"], again["attempt"])

    def test_new_head_increments_version_and_invalidates_evidence(self):
        self._seed_task()
        first = completion_runs.transition_completion_run(
            self._decision(), actor="test", project="switchboard")
        second = completion_runs.transition_completion_run(
            self._decision(
                head_sha="b" * 40,
                state="waiting",
                route="wait",
                reason_code="new_head_assessment",
                evidence_refs={"ci": {"head_sha": "b" * 40, "status": "pending"}},
            ),
            actor="test", project="switchboard")
        self.assertEqual(second["state_version"], first["state_version"] + 1)
        self.assertEqual(second["head_sha"], "b" * 40)
        self.assertNotIn("review", second.get("evidence_refs") or {})
        # Old-head evidence must not survive as authoritative.
        self.assertEqual(
            (second.get("evidence_refs") or {}).get("ci", {}).get("head_sha"),
            "b" * 40)

    def test_new_route_increments_state_version(self):
        self._seed_task()
        first = completion_runs.transition_completion_run(
            self._decision(), actor="test", project="switchboard")
        second = completion_runs.transition_completion_run(
            self._decision(
                state="blocked", route="remediation",
                reason_code="ci_failed", desired_role="remediation",
                board_status="Blocked"),
            actor="test", project="switchboard")
        self.assertEqual(second["state_version"], first["state_version"] + 1)

    def test_atomic_board_projection_and_history(self):
        self._seed_task()
        run = completion_runs.transition_completion_run(
            self._decision(
                state="blocked", route="remediation",
                reason_code="ci_failed", desired_role="remediation",
                board_status="Blocked",
                history_phase="ci",
                history_outcome="failed",
                history_failure={"code": "exact_head_ci_failed"},
            ),
            actor="test", project="switchboard")
        status = self.db.execute(
            "SELECT status FROM tasks WHERE task_id=?",
            ("SIMPLIFY-22",)).fetchone()["status"]
        self.assertEqual(status, "Blocked")
        history = task_completion.get_completion(
            "SIMPLIFY-22", pr_number=812, head_sha="a" * 40,
            runner_generation=3, project="switchboard")
        self.assertIsNotNone(history)
        self.assertEqual(history["phase"], "ci")
        self.assertEqual(history["outcome"], "failed")
        self.assertEqual(run["route"], "remediation")

    def test_crash_boundary_keeps_run_and_history_aligned(self):
        """If history write raises, the completion run must not advance alone."""
        self._seed_task()
        completion_runs.transition_completion_run(
            self._decision(), actor="test", project="switchboard")
        before = completion_runs.get_active_completion_run(
            "SIMPLIFY-22", project="switchboard")

        def boom(*_a, **_k):
            raise RuntimeError("simulated crash")

        with patch.object(completion_runs, "_append_history_in", side_effect=boom):
            with self.assertRaises(RuntimeError):
                completion_runs.transition_completion_run(
                    self._decision(
                        state="blocked", route="remediation",
                        reason_code="ci_failed", desired_role="remediation",
                        board_status="Blocked",
                        history_phase="ci",
                        history_outcome="failed",
                        history_failure={"code": "exact_head_ci_failed"},
                    ),
                    actor="test", project="switchboard")
        after = completion_runs.get_active_completion_run(
            "SIMPLIFY-22", project="switchboard")
        self.assertEqual(after["state_version"], before["state_version"])
        self.assertEqual(after["route"], before["route"])
        status = self.db.execute(
            "SELECT status FROM tasks WHERE task_id=?",
            ("SIMPLIFY-22",)).fetchone()["status"]
        self.assertEqual(status, "In Review")

    def test_recover_in_review_without_duplicate_effects(self):
        self._seed_task(status="In Review")
        first = completion_runs.recover_incomplete_runs(project="switchboard")
        self.assertEqual(first["recovered"], 1)
        run = completion_runs.get_active_completion_run(
            "SIMPLIFY-22", project="switchboard")
        self.assertIsNotNone(run)
        self.assertEqual(run["pr_number"], 812)
        second = completion_runs.recover_incomplete_runs(project="switchboard")
        self.assertEqual(second["recovered"], 0)
        again = completion_runs.get_active_completion_run(
            "SIMPLIFY-22", project="switchboard")
        self.assertEqual(again["run_id"], run["run_id"])
        self.assertEqual(again["state_version"], run["state_version"])

    def test_refuse_done_without_canonical_provenance(self):
        self._seed_task()
        completion_runs.transition_completion_run(
            self._decision(), actor="test", project="switchboard")
        with self.assertRaisesRegex(
                completion_runs.CompletionRunError, "canonical provenance"):
            completion_runs.transition_completion_run(
                self._decision(
                    state="done", route="none",
                    reason_code="merged",
                    board_status="Done",
                    evidence_refs={"note": "looks merged"}),
                actor="test", project="switchboard")
        status = self.db.execute(
            "SELECT status FROM tasks WHERE task_id=?",
            ("SIMPLIFY-22",)).fetchone()["status"]
        self.assertNotEqual(status, "Done")

    def test_done_with_canonical_provenance(self):
        self._seed_task()
        completion_runs.transition_completion_run(
            self._decision(), actor="test", project="switchboard")
        done = completion_runs.transition_completion_run(
            self._decision(
                state="done", route="none",
                reason_code="canonical_merge",
                board_status="Done",
                evidence_refs={
                    "merge": {
                        "merged_sha": "c" * 40,
                        "provenance_source": "github_pr_merged",
                        "repo_role": "canonical",
                    }
                }),
            actor="test", project="switchboard")
        self.assertEqual(done["state"], "done")
        status = self.db.execute(
            "SELECT status FROM tasks WHERE task_id=?",
            ("SIMPLIFY-22",)).fetchone()["status"]
        self.assertEqual(status, "Done")


class BlockedMergeEligibilityTest(unittest.TestCase):
    def test_orphan_eligible_statuses_include_blocked(self):
        import orphan_merge_discovery
        self.assertIn("Blocked", orphan_merge_discovery.ELIGIBLE_STATUSES)

    def test_recorded_pr_stamp_eligible_includes_blocked(self):
        from switchboard.storage.repositories import provenance
        self.assertTrue(
            provenance._recorded_pr_stamp_eligible(
                "Blocked", default_branch_merge=True, pr_names_task=True,
                has_merged_sha=False))
        self.assertTrue(
            provenance._recorded_pr_stamp_eligible(
                "In Review", default_branch_merge=True, pr_names_task=False,
                has_merged_sha=False))


if __name__ == "__main__":
    unittest.main()
