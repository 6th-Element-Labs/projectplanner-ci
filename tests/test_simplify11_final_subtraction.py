#!/usr/bin/env python3
"""SIMPLIFY-11 final subtraction ratchet."""
import json
from pathlib import Path

from path_setup import ROOT


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


for removed in (
    "coordinator_dispatch.py",
    "review_steward.py",
    "merge_steward.py",
    "deploy/projectplanner-coordinator-dispatch.service",
    "deploy/projectplanner-coordinator-dispatch.timer",
    "src/switchboard/application/commands/request_wake.py",
):
    assert not (ROOT / removed).exists(), removed

task_execution = read("src/switchboard/application/commands/task_execution.py")
tasks_router = read("src/switchboard/api/routers/tasks.py")
runner_router = read("src/switchboard/api/routers/runner.py")
wakes_router = read("src/switchboard/api/routers/wakes.py")
runner_tools = read("src/switchboard/mcp/tools/runner.py")
wake_tools = read("src/switchboard/mcp/tools/wakes.py")
agent_host = read("adapters/agent_host.py")
connect = read("src/switchboard/application/commands/connect_dispatch.py")
daemon = read("coordinator_daemon.py")
scoped = read("scoped_completion_coordinator.py")

assert "connect_dispatch.enqueue_task" in task_execution
for route in (
    "/api/tasks/{task_id}/start",
    "/api/tasks/{task_id}/execution",
    "/api/tasks/{task_id}/execution/open",
    "/api/tasks/{task_id}/execution/message",
    "/api/tasks/{task_id}/execution/stop",
    "/api/tasks/{task_id}/execution/retry",
):
    assert route in tasks_router

for side_door in (
    "/ixp/v1/runner_sessions/watch",
    "/ixp/v1/request_runner_",
):
    assert side_door not in runner_router
for side_door in ("/txp/v1/request_wake", "/txp/v1/cancel_wake",
                  "/ixp/v1/request_wake", "/ixp/v1/cancel_wake"):
    assert side_door not in wakes_router
for name in ("resolve_runner_watch", "request_runner_kill",
             "request_runner_inject", "mint_runner_pty_ticket"):
    assert name not in runner_tools
for name in ("def request_wake(", "def cancel_wake("):
    assert name not in wake_tools

assert "PM_RUNNER_LEASE_ENFORCEMENT" not in agent_host
assert "PM_RUNNER_LEASE_ENFORCEMENT" not in connect
assert "def reap_finished_or_idle_runners" not in agent_host
assert agent_host.count("def expire_runner_leases(") == 1
assert '"execution_lease_v2", "runner_lease_enforcement"' in connect

assert "janitor" in daemon.lower()
assert "self.store.run_mission_coordinator_tick" in scoped

baseline = json.loads(read("perf/simplify10_execution_authority_baseline.json"))
for scope in (
    "wake_assembly_outside_service",
    "legacy_launcher_calls_outside_service",
    "host_selection_outside_service",
    "assignment_authoring_outside_service",
    "runner_resolution_outside_service",
    "browser_execution_facts",
    "task_execution_surface",
):
    assert baseline["scopes"][scope]["ceiling"] == 0, scope

adr = read("docs/decisions/0008-three-plane-separation.md")
assert "SIMPLIFY-15" in adr and "one completion and merge owner" in adr.lower()

print("SIMPLIFY-11 final subtraction ratchet: PASS")
