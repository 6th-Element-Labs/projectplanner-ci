"""ADAPTER-32 / BUG-195: Connect register transport failures must not strand wakes.

A TLS handshake timeout on register_runner_session was mislabeled failed_gate
("Connect runner registry rejected the launch"), the runner was killed, and
complete_wake was _try-skipped — leaving the wake claimed forever.
"""
from __future__ import annotations

import unittest
import urllib.error
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

import adapters.agent_host as agent_host


class RequireTransportClassificationTest(unittest.TestCase):
    def test_require_marks_urlerror_as_broken_connection(self):
        def boom(method, path, body=None):
            raise urllib.error.URLError("The handshake operation timed out")

        with patch.object(agent_host.sb, "_http", side_effect=boom):
            result = agent_host._require("POST", agent_host.P_REGISTER_RUNNER, {})

        self.assertEqual(result.get("failure_class"), "broken_connection")
        self.assertEqual(result.get("error_code"), "control_plane_unavailable")
        self.assertTrue(result.get("transport_error"))
        self.assertNotEqual(result.get("error_code"), "runner_bind_incomplete")
        self.assertFalse(result.get("refused"))

    def test_require_marks_http_403_registry_refusal_as_failed_gate(self):
        """Narrow-host register refusals raise RuntimeError(HTTP 403...), not a 200 body."""
        def boom(method, path, body=None):
            raise RuntimeError(
                "HTTP 403 /ixp/v1/register_runner_session: "
                "error_code=runner_bind_incomplete; message=missing claim_id"
            )

        with patch.object(agent_host.sb, "_http", side_effect=boom):
            result = agent_host._require("POST", agent_host.P_REGISTER_RUNNER, {})

        self.assertEqual(result.get("error_code"), "runner_bind_incomplete")
        self.assertTrue(result.get("refused"))
        self.assertFalse(result.get("transport_error"))
        classified = agent_host._classify_connect_registration_failure(result)
        self.assertEqual(classified["failure_class"], "failed_gate")
        self.assertEqual(
            classified["reason"], "connect_runner_registration_failed")


class ConnectRegistrationFailureClassificationTest(unittest.TestCase):
    def test_transport_error_is_not_failed_gate(self):
        classified = agent_host._classify_connect_registration_failure({
            "error": "control_plane_unavailable",
            "error_code": "control_plane_unavailable",
            "failure_class": "broken_connection",
            "transport_error": True,
            "message": "POST /ixp/v1/register_runner_session failed: URLError",
        })
        self.assertEqual(classified["failure_class"], "broken_connection")
        self.assertEqual(
            classified["reason"], "connect_runner_registration_transport_failed")
        self.assertNotIn("rejected the launch", classified["provider_error"])

    def test_registry_refusal_remains_failed_gate(self):
        classified = agent_host._classify_connect_registration_failure({
            "error": "runner_bind_incomplete",
            "error_code": "runner_bind_incomplete",
            "failure_class": "unbound_identity",
            "refused": True,
            "message": "missing claim_id",
        })
        self.assertEqual(classified["failure_class"], "failed_gate")
        self.assertEqual(classified["reason"], "connect_runner_registration_failed")
        self.assertEqual(
            classified["provider_error"],
            "Connect runner registry rejected the launch",
        )


class ConnectRegisterRetryAndCompleteWakeTest(unittest.TestCase):
    def test_connect_retries_register_then_completes_wake_after_transport_blip(self):
        wake = {
            "wake_id": "wake-adapter32",
            "task_id": "ADAPTER-32",
            "selector": {
                "agent_id": "codex/ADAPTER-32",
                "runtime": "codex",
                "lane": "ADAPTER",
                "task_id": "ADAPTER-32",
            },
            "policy": {
                "mode": "connect",
                "repository_binding": {
                    "schema": "switchboard.repository_binding.v1",
                    "project": "switchboard",
                    "repo_role": "canonical",
                    "repository": "6th-Element-Labs/projectplanner",
                },
                "assignment": {
                    "schema": "switchboard.connect.assignment.v1",
                    "assignment_id": "assignment-adapter32",
                    "runtime": "codex",
                    "provider": "openai",
                    "work_ref": "task:switchboard:ADAPTER-32",
                    "workspace_ref": "repo:canonical",
                },
                "lifecycle": {
                    "schema": "switchboard.execution_lifecycle.v1",
                    "execution_id": "execlease-adapter32",
                    "generation": 1,
                    "role": "implementation",
                    "ttl_seconds": 7200,
                },
            },
        }
        inventory = {
            "host_id": "host/test-adapter32",
            "repo_root": str(ROOT),
            "limits": {"max_sessions": 8},
            "policy": {
                "allow_work": True,
                "allow_global_claim": False,
                "lane_mode": "all_project_lanes",
            },
            "runtimes": [{
                "runtime": "codex",
                "lanes": [],
                "capabilities": ["execution_lease_v2", "runner_lease_enforcement"],
                "provider": "openai",
                "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
            }],
        }
        posts = []
        register_calls = {"n": 0}
        kills = []

        def fake_try(method, path, body=None):
            posts.append((method, path, dict(body or {})))
            if path.startswith(agent_host.P_LIST_WAKES):
                return {"wake_intents": [wake]}
            if path == agent_host.P_CLAIM_WAKE:
                return {"claimed": True, "wake": wake}
            if path == agent_host.P_COMPLETE_WAKE:
                # First complete_wake fails like the live RuntimeError skip.
                if sum(1 for p in posts if p[1] == agent_host.P_COMPLETE_WAKE) == 1:
                    return None
                return {"ok": True, "wake_id": wake["wake_id"], "status": "failed"}
            return {"ok": True}

        def fake_require(method, path, body=None):
            posts.append((method, path, dict(body or {})))
            if path == agent_host.P_REGISTER_RUNNER:
                register_calls["n"] += 1
                if register_calls["n"] == 1:
                    return {
                        "error": "control_plane_unavailable",
                        "error_code": "control_plane_unavailable",
                        "failure_class": "broken_connection",
                        "transport_error": True,
                        "refused": False,
                        "message": (
                            "POST /ixp/v1/register_runner_session failed: "
                            "URLError: The handshake operation timed out"
                        ),
                    }
                return {
                    "runner_session_id": (body or {}).get("runner_session_id"),
                    "ok": True,
                }
            return {"ok": True}

        def fake_launch(selected, inv, runner_session_id="", extra_env=None):
            return {
                "runner_session_id": runner_session_id or "run_adapter32",
                "agent_id": "codex/ADAPTER-32",
                "runtime": "codex",
                "task_id": "ADAPTER-32",
                "pid": 4242,
                "status": "running",
                "cwd": str(ROOT),
                "control": {
                    "tier": "T3",
                    "runner_kill": True,
                    "managed_process": True,
                },
                "wake_mode": "connect",
            }

        with patch.object(agent_host, "_try", side_effect=fake_try), \
                patch.object(agent_host, "_require", side_effect=fake_require), \
                patch.object(agent_host, "launch", side_effect=fake_launch), \
                patch.object(agent_host, "confirm_started", return_value=True), \
                patch.object(agent_host, "active_session_count", return_value=0), \
                patch.object(
                    agent_host, "supervisor_action",
                    side_effect=lambda action, rid, options=None: kills.append(
                        (action, rid, options or {})) or {"ok": True}), \
                patch.object(agent_host, "_reap_bound_finalizers", return_value=[]), \
                patch.object(
                    agent_host, "heartbeat_capacity",
                    return_value={"active_sessions": 0, "local_auth": {"available": True}}), \
                patch.object(
                    agent_host, "apply_authoritative_execution_policy",
                    return_value=False), \
                patch.object(agent_host, "handle_runner_controls", return_value=[]), \
                patch.object(agent_host.time, "sleep", return_value=None):
            summary = agent_host.run_once(inventory)

        acted = summary.get("acted") or []
        self.assertEqual(len(acted), 1, summary)
        self.assertTrue(acted[0].get("started"), acted[0])
        self.assertTrue(acted[0].get("runner_registered"), acted[0])
        self.assertEqual(register_calls["n"], 2)
        self.assertEqual(kills, [])
        complete_posts = [p for p in posts if p[1] == agent_host.P_COMPLETE_WAKE]
        # Retried until recorded; first None must not strand the wake.
        self.assertGreaterEqual(len(complete_posts), 2, posts)
        self.assertTrue(complete_posts[-1][2].get("result", {}).get("started"))

    def test_connect_transport_exhaustion_completes_wake_as_broken_connection(self):
        wake = {
            "wake_id": "wake-adapter32-fail",
            "task_id": "ADAPTER-32",
            "selector": {
                "agent_id": "codex/ADAPTER-32",
                "runtime": "codex",
                "lane": "ADAPTER",
            },
            "policy": {
                "mode": "connect",
                "repository_binding": {
                    "schema": "switchboard.repository_binding.v1",
                    "project": "switchboard",
                    "repo_role": "canonical",
                    "repository": "6th-Element-Labs/projectplanner",
                },
                "assignment": {
                    "schema": "switchboard.connect.assignment.v1",
                    "assignment_id": "assignment-adapter32b",
                    "runtime": "codex",
                    "provider": "openai",
                },
                "lifecycle": {
                    "schema": "switchboard.execution_lifecycle.v1",
                    "execution_id": "execlease-adapter32b",
                    "generation": 1,
                    "role": "implementation",
                },
            },
        }
        inventory = {
            "host_id": "host/test-adapter32",
            "repo_root": str(ROOT),
            "limits": {"max_sessions": 8},
            "policy": {
                "allow_work": True,
                "allow_global_claim": False,
                "lane_mode": "all_project_lanes",
            },
            "runtimes": [{
                "runtime": "codex",
                "lanes": [],
                "capabilities": ["execution_lease_v2"],
                "provider": "openai",
                "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
            }],
        }
        posts = []

        def fake_try(method, path, body=None):
            posts.append((method, path, dict(body or {})))
            if path.startswith(agent_host.P_LIST_WAKES):
                return {"wake_intents": [wake]}
            if path == agent_host.P_CLAIM_WAKE:
                return {"claimed": True, "wake": wake}
            if path == agent_host.P_COMPLETE_WAKE:
                return {"ok": True, "status": "failed"}
            return {"ok": True}

        def always_transport(method, path, body=None):
            posts.append((method, path, dict(body or {})))
            return {
                "error": "control_plane_unavailable",
                "error_code": "control_plane_unavailable",
                "failure_class": "broken_connection",
                "transport_error": True,
                "refused": False,
                "message": "POST failed: URLError",
            }

        with patch.object(agent_host, "_try", side_effect=fake_try), \
                patch.object(agent_host, "_require", side_effect=always_transport), \
                patch.object(
                    agent_host, "launch",
                    return_value={
                        "runner_session_id": "run_fail",
                        "pid": 99,
                        "status": "running",
                        "cwd": str(ROOT),
                        "wake_mode": "connect",
                        "task_id": "ADAPTER-32",
                    }), \
                patch.object(agent_host, "confirm_started", return_value=True), \
                patch.object(agent_host, "active_session_count", return_value=0), \
                patch.object(agent_host, "supervisor_action", return_value={"ok": True}), \
                patch.object(agent_host, "_reap_bound_finalizers", return_value=[]), \
                patch.object(
                    agent_host, "heartbeat_capacity",
                    return_value={"active_sessions": 0}), \
                patch.object(
                    agent_host, "apply_authoritative_execution_policy",
                    return_value=False), \
                patch.object(agent_host, "handle_runner_controls", return_value=[]), \
                patch.object(agent_host.time, "sleep", return_value=None):
            summary = agent_host.run_once(inventory)

        acted = (summary.get("acted") or [None])[0]
        self.assertIsNotNone(acted)
        self.assertFalse(acted.get("started"))
        self.assertEqual(acted.get("failure_class"), "broken_connection")
        self.assertEqual(
            acted.get("reason"), "connect_runner_registration_transport_failed")
        self.assertNotEqual(acted.get("failure_class"), "failed_gate")
        complete = [p for p in posts if p[1] == agent_host.P_COMPLETE_WAKE]
        self.assertEqual(len(complete), 1)
        self.assertEqual(
            (complete[0][2].get("result") or {}).get("failure_class"),
            "broken_connection",
        )


class CompleteWakeRetryHelperTest(unittest.TestCase):
    def test_complete_wake_retries_until_recorded(self):
        attempts = {"n": 0}

        def flaky(method, path, body=None):
            attempts["n"] += 1
            if path != agent_host.P_COMPLETE_WAKE:
                return {"ok": True}
            if attempts["n"] < 3:
                return None
            return {"ok": True, "status": "failed"}

        with patch.object(agent_host, "_try", side_effect=flaky), \
                patch.object(agent_host.time, "sleep", return_value=None):
            result = agent_host._complete_wake_with_retry({
                "project": "switchboard",
                "wake_id": "wake-x",
                "runner_session_id": "run-x",
                "agent_id": "codex/x",
                "result": {"started": False, "reason": "broken"},
            })

        self.assertTrue(result and result.get("ok"))
        self.assertEqual(attempts["n"], 3)


if __name__ == "__main__":
    unittest.main()
