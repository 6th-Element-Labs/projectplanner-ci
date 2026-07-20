"""SIMPLIFY-3 / BUG-102: claimed wake limbo ends in the same tick as local death."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

import adapters.agent_host as agent_host


class ClaimedWakeLimboRepairTest(unittest.TestCase):
    def test_death_tick_completes_wake_with_local_reason(self):
        inventory = {"host_id": "host/mac", "repo_root": "/tmp/repo"}
        # Still status=running centrally/locally, but supervisor says dead —
        # the BUG-102 hole is that complete_wake never fires and the wake
        # stays claimed. Same-tick repair closes it.
        session = {
            "runner_session_id": "run-dead",
            "task_id": "BUG-102",
            "agent_id": "codex/BUG-102",
            "runtime": "codex",
            "status": "running",
            "alive": False,
            "claim_id": "",
            "wake_id": "wake-stuck",
            "metadata": {
                "wake_id": "wake-stuck",
                "direct_assignment": True,
                "failure_reason": "CLI exited before bind",
            },
        }
        posts = []

        def fake_try(method, path, body=None, **kwargs):
            posts.append((method, path, body or {}))
            if path == agent_host.P_COMPLETE_WAKE:
                return {"ok": True, "wake_id": "wake-stuck", "status": "failed"}
            if path == agent_host.P_HEARTBEAT_RUNNER:
                return {"ok": True, "status": "exited"}
            return {"ok": True}

        with patch.object(agent_host, "_drain_runners", return_value=[session]), \
                patch.object(agent_host, "_drain_work_sessions", return_value=[]), \
                patch.object(agent_host, "_try", side_effect=fake_try):
            renewed = agent_host.renew_live_direct_runners(inventory)

        heartbeats = [p for p in posts if p[1] == agent_host.P_HEARTBEAT_RUNNER]
        complete = [p for p in posts if p[1] == agent_host.P_COMPLETE_WAKE]
        self.assertEqual(len(heartbeats), 1, posts)
        self.assertEqual(heartbeats[0][2].get("status"), "exited")
        self.assertEqual(
            (heartbeats[0][2].get("metadata") or {}).get("failure_reason"),
            "CLI exited before bind",
        )
        self.assertEqual(len(complete), 1, posts)
        result = complete[0][2].get("result") or {}
        self.assertIs(result.get("started"), False)
        self.assertEqual(result.get("reason"), "CLI exited before bind")
        self.assertTrue(renewed[0].get("terminalized"))
        self.assertTrue(renewed[0].get("wake_repaired"))


if __name__ == "__main__":
    unittest.main()
