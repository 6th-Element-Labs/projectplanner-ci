from unittest import TestCase, main, mock

from path_setup import ROOT  # noqa: F401

from adapters import agent_host


class MultiProjectWakePollingTests(TestCase):
    def test_run_once_polls_every_registered_project(self):
        calls = []

        def fake_try(method, path, body=None):
            calls.append((method, path, body or {}))
            if path.startswith(agent_host.P_LIST_WAKES):
                project = "atlas" if "project=atlas" in path else "switchboard"
                return {"wake_intents": [{
                    "wake_id": f"wake-{project}",
                    "selector": {"host_id": "host/test"},
                }]}
            return {"ok": True}

        inventory = {
            "host_id": "host/test",
            "placement": {"projects": ["atlas", "switchboard"]},
            "limits": {"max_sessions": 0},
        }
        with (
            mock.patch.object(agent_host, "_try", side_effect=fake_try),
            mock.patch.object(agent_host.co_drain, "discover_request", return_value=None),
            mock.patch.object(agent_host, "_reap_bound_finalizers", return_value=[]),
            mock.patch.object(
                agent_host, "heartbeat_capacity",
                return_value={
                    "active_sessions": 0,
                    "local_auth": {"available": True},
                },
            ),
            mock.patch.object(
                agent_host, "apply_authoritative_execution_policy",
                return_value=False,
            ),
            mock.patch.object(agent_host, "expire_runner_leases", return_value=[]),
            mock.patch.object(
                agent_host, "renew_live_direct_runners", return_value=[]),
            mock.patch.object(agent_host, "handle_runner_controls", return_value=[]),
            mock.patch.object(agent_host, "active_session_count", return_value=0),
        ):
            summary = agent_host.run_once(inventory)

        list_paths = [
            path for method, path, _body in calls
            if method == "GET" and path.startswith(agent_host.P_LIST_WAKES)
        ]
        self.assertEqual(list_paths, [
            f"{agent_host.P_LIST_WAKES}?project=atlas&status=pending",
            f"{agent_host.P_LIST_WAKES}?project=switchboard&status=pending",
        ])
        self.assertEqual(summary["pending"], 2)

    def test_switchboard_release_response_drives_multi_project_host_update(self):
        release = {
            "version": "0.4.49",
            "bundle_digest": "sha256:new",
            "contract_fingerprint": "eac1:new",
            "download_url": "https://plan.example/release",
        }
        update_responses = []

        def fake_try(method, path, body=None):
            body = body or {}
            if path == agent_host.P_HEARTBEAT_HOST:
                return {
                    "required_host_release": release
                    if body.get("project") == "switchboard" else {},
                    "project": body.get("project"),
                }
            if path.startswith(agent_host.P_LIST_WAKES):
                return {"wake_intents": []}
            return {"ok": True}

        inventory = {
            "host_id": "host/test",
            "placement": {"projects": ["maxwell", "switchboard"]},
            "limits": {"max_sessions": 0},
        }
        with (
            mock.patch.object(agent_host, "PROJECT", "maxwell"),
            mock.patch.dict(
                agent_host.os.environ,
                {"PM_AGENT_HOST_RELEASE_PROJECT": ""},
            ),
            mock.patch.object(agent_host, "_try", side_effect=fake_try),
            mock.patch.object(agent_host.co_drain, "discover_request", return_value=None),
            mock.patch.object(agent_host, "_reap_bound_finalizers", return_value=[]),
            mock.patch.object(
                agent_host, "heartbeat_capacity",
                return_value={
                    "active_sessions": 0,
                    "local_auth": {"available": True},
                },
            ),
            mock.patch.object(
                agent_host, "apply_authoritative_execution_policy",
                return_value=False,
            ) as policy,
            mock.patch.object(
                agent_host, "apply_required_host_release",
                side_effect=lambda _inventory, response, _capacity: (
                    update_responses.append(response) or {"phase": "installing"}
                ),
            ),
        ):
            summary = agent_host.run_once(inventory)

        self.assertEqual(release, update_responses[0]["required_host_release"])
        self.assertEqual("installing", summary["host_update"]["phase"])
        self.assertEqual("maxwell", policy.call_args.args[1]["project"])

    def test_wake_project_stays_bound_to_poll_source(self):
        self.assertEqual(agent_host._wake_project({
            "_host_project": "atlas",
            "project": "switchboard",
        }), "atlas")

    def test_connect_session_token_uses_wake_project(self):
        calls = []

        def fake_http(method, path, body=None):
            calls.append((method, path, body or {}))
            return {"issued": True, "token": "dst-switchboard"}

        wake = {
            "_host_project": "switchboard",
            "wake_id": "wake-switchboard",
        }
        inventory = {"host_id": "host/test"}
        with (
            mock.patch.object(agent_host, "PROJECT", "atlas"),
            mock.patch.object(agent_host.sb, "_http", side_effect=fake_http),
        ):
            token = agent_host._issue_connect_session_mcp_token(
                wake, inventory, "run-switchboard",
            )

        self.assertEqual(token, "dst-switchboard")
        self.assertEqual(calls, [(
            "POST",
            agent_host.P_DIRECT_SESSION_MCP_TOKEN,
            {
                "project": "switchboard",
                "wake_id": "wake-switchboard",
                "host_id": "host/test",
                "runner_session_id": "run-switchboard",
            },
        )])

    def test_runner_registration_uses_wake_project(self):
        calls = []

        def fake_try(method, path, body=None):
            calls.append((method, path, body or {}))
            return {"ok": True}

        wake = {
            "_host_project": "atlas",
            "wake_id": "wake-atlas",
            "task_id": "CORE-6",
            "selector": {"agent_id": "agent/codex/core-6", "runtime": "codex"},
        }
        rec = {"runner_session_id": "run-atlas", "status": "running"}
        inventory = {"host_id": "host/test", "repo_root": "/tmp/atlas"}
        with (
            mock.patch.object(agent_host, "_try", side_effect=fake_try),
            mock.patch.object(agent_host, "_host_repo_preflight", return_value=None),
        ):
            agent_host.register_runner_session(rec, wake, inventory)

        register = next(
            body for method, path, body in calls
            if method == "POST" and path == agent_host.P_REGISTER_RUNNER
        )
        self.assertEqual(register["project"], "atlas")


if __name__ == "__main__":
    main()
