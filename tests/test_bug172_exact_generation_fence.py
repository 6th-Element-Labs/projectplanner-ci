"""BUG-172: a stale completion tick cannot fence a newer execution."""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import task_execution
from switchboard.storage.repositories import runner


HEAD_OLD = "a" * 40
HEAD_NEW = "b" * 40


def identity(prefix: str, generation: int, epoch: int, head: str) -> dict:
    return {
        "runner_session_id": f"runner-{prefix}",
        "execution_id": f"execution-{prefix}",
        "execution_connection_id": f"connection-{prefix}",
        "generation": generation,
        "fence_epoch": epoch,
        "role": "review_merge",
        "head_sha": head,
    }


class ExactGenerationFence(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE runner_sessions (
                runner_session_id TEXT PRIMARY KEY,
                task_id TEXT,
                metadata_json TEXT,
                heartbeat_at REAL,
                heartbeat_ttl_s INTEGER,
                updated_at REAL
            );
            CREATE TABLE resource_leases (
                id TEXT PRIMARY KEY,
                resource_type TEXT,
                execution_generation INTEGER,
                fence_epoch INTEGER,
                lease_state TEXT,
                ttl_seconds INTEGER,
                released_at REAL,
                claimed_at REAL
            );
            CREATE TABLE direct_session_tokens (
                runner_session_id TEXT,
                revoked_at REAL
            );
            CREATE TABLE activity (
                task_id TEXT,
                actor TEXT,
                kind TEXT,
                payload TEXT,
                created_at REAL
            );
            """
        )

    def tearDown(self):
        self.db.close()

    def insert_generation(self, expected: dict):
        metadata = {
            "execution_id": expected["execution_id"],
            "execution_connection_id": expected["execution_connection_id"],
            "execution_generation": expected["generation"],
            "execution_role": expected["role"],
            "execution_head_sha": expected["head_sha"],
            "lease_epoch": expected["fence_epoch"],
        }
        self.db.execute(
            "INSERT INTO runner_sessions VALUES (?,?,?,?,?,?)",
            (
                expected["runner_session_id"],
                "COORD-46",
                json.dumps(metadata),
                1000.0,
                60,
                1000.0,
            ),
        )
        self.db.execute(
            "INSERT INTO resource_leases VALUES (?,?,?,?,?,?,?,?)",
            (
                expected["execution_id"],
                "execution",
                expected["generation"],
                expected["fence_epoch"],
                "active",
                60,
                None,
                1000.0,
            ),
        )
        self.db.commit()

    def test_generation_changed_after_plan_refuses_without_mutation(self):
        planned = identity("shared", 1, 1, HEAD_OLD)
        current = identity("shared", 2, 2, HEAD_NEW)
        self.insert_generation(current)
        with patch.object(runner, "_conn", return_value=self.db):
            result = runner.make_runner_lease_due(
                current["runner_session_id"],
                reason="stale completion plan",
                authority="completion_owner",
                actor="completion-owner",
                project="switchboard",
                expected_identity=planned,
            )
        self.assertFalse(result["updated"])
        self.assertEqual(result["error"], "execution_identity_mismatch")
        self.assertIn("generation", result["mismatched_fields"])
        lease = self.db.execute(
            "SELECT lease_state,fence_epoch FROM resource_leases"
        ).fetchone()
        self.assertEqual((lease["lease_state"], lease["fence_epoch"]), ("active", 2))
        metadata = json.loads(self.db.execute(
            "SELECT metadata_json FROM runner_sessions"
        ).fetchone()[0])
        self.assertNotIn("lease_surrender", metadata)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM activity").fetchone()[0], 0)

    def test_exact_old_generation_never_fences_newer_generation(self):
        old = identity("old", 1, 1, HEAD_OLD)
        new = identity("new", 2, 1, HEAD_NEW)
        self.insert_generation(old)
        self.insert_generation(new)
        with patch.object(runner, "_conn", return_value=self.db):
            result = runner.make_runner_lease_due(
                old["runner_session_id"],
                reason="replace old exact generation",
                authority="completion_owner",
                actor="completion-owner",
                project="switchboard",
                expected_identity=old,
            )
        self.assertTrue(result["updated"])
        leases = {
            row["id"]: (row["lease_state"], row["fence_epoch"])
            for row in self.db.execute(
                "SELECT id,lease_state,fence_epoch FROM resource_leases"
            )
        }
        self.assertEqual(leases[old["execution_id"]], ("stopping", 2))
        self.assertEqual(leases[new["execution_id"]], ("active", 1))

    def test_command_requires_full_server_owned_identity(self):
        with self.assertRaises(task_execution.TaskExecutionError) as caught:
            task_execution.fence_task_generation(
                "COORD-46",
                {"runner_session_id": "runner-only"},
                project="switchboard",
                actor="completion-owner",
            )
        self.assertEqual(caught.exception.code, "runner_bind_incomplete")


if __name__ == "__main__":
    unittest.main()
