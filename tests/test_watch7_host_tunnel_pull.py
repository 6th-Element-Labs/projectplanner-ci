#!/usr/bin/env python3
"""WATCH-7: Agent Host pulls a fresh ticket when bridge options omit one."""
from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from switchboard.application.commands import runner_control
from switchboard.storage.repositories import runner as runner_repo


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


calls = []
saved_try = agent_host._try
try:
    agent_host._try = lambda method, path, body=None: (
        calls.append((method, path, body)) or {
            "server_relay": {
                "host_url": "wss://plan.example/ixp/v1/runner_sessions/run-watch7/pty/host?ticket=fresh",
                "binding": {"runner_session_id": "run-watch7", "host_id": "host/watch7"},
            },
        })
    relay = agent_host._fresh_server_relay({}, "run-watch7", "host/watch7")
finally:
    agent_host._try = saved_try

ok(relay.get("host_url", "").endswith("ticket=fresh"),
   "a missing bridge capability is replaced by a freshly pulled URL")
ok(calls == [("POST", agent_host.P_MINT_HOST_TUNNEL_URL, {
    "project": agent_host.PROJECT,
    "runner_session_id": "run-watch7",
    "host_id": "host/watch7",
})], "the pull is bound to the exact project, host, and runner")

calls.clear()
existing = {"host_url": "wss://plan.example/existing"}
agent_host._try = lambda *args, **kwargs: calls.append((args, kwargs))
try:
    reused = agent_host._fresh_server_relay(existing, "run-watch7", "host/watch7")
finally:
    agent_host._try = saved_try
ok(reused == existing and not calls,
   "a still-present host URL is reused without an unnecessary mint request")

session = {"runner_session_id": "run-watch7", "host_id": "host/watch7"}
recorded = []
saved_repo = {
    "get": runner_repo.get_runner_session,
    "mint": runner_repo._server_relay_options,
    "record": runner_repo.record_server_relay_failure,
}
try:
    runner_repo.get_runner_session = lambda *_args, **_kwargs: dict(session)
    runner_repo._server_relay_options = lambda current, **kwargs: {
        "host_url": "wss://plan.example/fresh",
        "binding": {"runner_session_id": current["runner_session_id"]},
    }
    runner_repo.record_server_relay_failure = (
        lambda *args, **kwargs: recorded.append((args, kwargs)))
    command = runner_control.mint_host_tunnel_url_mapping_result(
        {"project": "switchboard", "runner_session_id": "run-watch7"},
        actor="host/watch7", principal_id="principal/watch7")
finally:
    runner_repo.get_runner_session = saved_repo["get"]
    runner_repo._server_relay_options = saved_repo["mint"]
    runner_repo.record_server_relay_failure = saved_repo["record"]

ok(command.get("server_relay", {}).get("host_url", "").endswith("/fresh"),
   "the server command mints through the existing relay authorization path")
ok(not recorded, "a successful pull does not emit a relay failure event")

print("\n%d passed, %d failed" % (passed, failed))
raise SystemExit(1 if failed else 0)
