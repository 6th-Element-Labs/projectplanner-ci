#!/usr/bin/env python3
"""ADAPTER-34: Connect launch must fail closed if workspace materialize hangs."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

import adapters.agent_host as agent_host


class LaunchMaterializeTimeoutTest(unittest.TestCase):
    def test_materialize_timeout_returns_started_false(self):
        wake = {
            "wake_id": "wake-hang",
            "task_id": "COORD-63",
            "selector": {"runtime": "codex", "task_id": "COORD-63"},
            "policy": {
                "mode": "connect",
                "assignment": {
                    "assignment_id": "assignment-1",
                    "principal_ref": "agent/codex/coord-63",
                    "work_ref": "task:switchboard:COORD-63",
                    "workspace_ref": "repo:canonical",
                },
                "execution_assignment": {"task_id": "COORD-63", "desired_role": "remediation"},
                "execution_context": {
                    "schema": "switchboard.execution_context.v1",
                    "repository": "6th-Element-Labs/projectplanner",
                },
            },
        }
        inventory = {"host_id": "host/mac", "repo_root": "/tmp/repo"}

        def hang(timeout_s=None, **_kwargs):
            import time
            time.sleep(float(timeout_s or 0))
            raise agent_host.WorkspaceMaterializationError(
                "workspace_materialize_timeout",
                "repository workspace materialization deadline expired")

        with patch.object(agent_host, "wake_mode", return_value="connect"), \
                patch.object(agent_host, "connect_workspace_request",
                             return_value={"repo": "x", "dest": "/tmp/x"}), \
                patch.object(agent_host, "materialize_repository_workspace",
                             side_effect=hang), \
                patch.object(agent_host, "_try", return_value={"ok": True}), \
                patch.dict(agent_host.os.environ,
                           {"PM_CONNECT_MATERIALIZE_TIMEOUT_SECONDS": "0.2"}):
            rec = agent_host.launch(wake, inventory)

        self.assertIs(rec.get("started"), False)
        self.assertEqual(rec.get("reason"), "workspace_materialize_timeout")
        self.assertEqual(rec.get("failure_class"), "failed_gate")
        self.assertEqual(rec.get("wake_mode"), "connect")


if __name__ == "__main__":
    unittest.main()
