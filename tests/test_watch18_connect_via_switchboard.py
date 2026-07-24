#!/usr/bin/env python3
"""WATCH-18: Connect Start uses via-Switchboard note + required Codex MCP."""

from __future__ import annotations

import json
import os

from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from switchboard.connect import Ack, Assignment, HostRuntimeConfig, ResourceLimits
from switchboard.connect.execution_assignment import build_execution_assignment
from switchboard.connect.launcher import assignment_note, build_launch_spec


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


ack = Ack(
    lease_id="wake-watch18",
    runner_id="run_watch18",
    assignment=Assignment(
        assignment_id="assignment-watch18",
        principal_ref="agent/codex/watch-18",
        work_ref="task:switchboard:WATCH-18",
        runtime="codex",
        provider="openai",
        workspace_ref="repo:canonical",
        limits=ResourceLimits(
            max_runtime_seconds=7200,
            spend_limit_microunits=0,
            memory_limit_bytes=0,
        ),
        queued_at=1.0,
    ),
    host_id="host/watch18",
    issued_at=1.0,
    expires_at=7201.0,
    heartbeat_interval_seconds=30,
    last_heartbeat_at=1.0,
)

note = assignment_note(ack)
ok("Do WATCH-18 in project switchboard via Switchboard." in note,
   "note matches the working Direct via-Switchboard sentence")
ok("agent_id=agent/codex/watch-18" in note
   and "prepare_agent_session" in note
   and "register_agent" in note
   and "claims" in note,
   "note pins the exact agent_id for the Switchboard handshake")
ok("Use the Switchboard connection already configured" not in note,
   "vague host-connection wording is gone")

spec = build_launch_spec(
    ack,
    HostRuntimeConfig(
        runtime="codex",
        provider="openai",
        executable="codex",
        arguments_before_note=("--dangerously-bypass-approvals-and-sandbox",),
    ),
    workspace_path=str(ROOT),
)
ok(set(spec.env_dict()) == {
    "SWITCHBOARD_CONNECT_ASSIGNMENT_ID",
    "SWITCHBOARD_CONNECT_LEASE_ID",
    "SWITCHBOARD_CONNECT_PRINCIPAL_REF",
    "SWITCHBOARD_CONNECT_RUNNER_ID",
    "SWITCHBOARD_CONNECT_WORK_REF",
    "SWITCHBOARD_CONNECT_WORKSPACE_REF",
} and all("TOKEN" not in key and "MCP" not in key for key in spec.env_dict()),
   "Connect LaunchSpec stays metadata-only (host attaches Communicate)")

wake = {
    "wake_id": "wake-watch18",
    "task_id": "WATCH-18",
    "selector": {
        "runtime": "codex",
        "provider": "openai",
        "task_id": "WATCH-18",
        "agent_id": "agent/codex/watch-18",
    },
    "policy": {
        "mode": "connect",
        "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            "assignment_id": "assignment-watch18",
            "principal_ref": "agent/codex/watch-18",
            "work_ref": "task:switchboard:WATCH-18",
            "runtime": "codex",
            "provider": "openai",
            "workspace_ref": "repo:canonical",
            "queued_at": 1.0,
            "limits": {
                "max_runtime_seconds": 7200,
                "spend_limit_microunits": 0,
                "memory_limit_bytes": 0,
            },
        },
        "lifecycle": {
            "schema": "switchboard.execution_lifecycle.v1",
            "role": "implementation", "head_sha": "",
            "pr_number": 0, "pr_url": "", "ttl_seconds": 7200,
            "execution_id": "execlease-watch18",
            "generation": 1, "fence_epoch": 1,
        },
    },
}
wake["policy"]["execution_assignment"] = build_execution_assignment(
    task_id=wake["task_id"],
    assignment=wake["policy"]["assignment"],
    lifecycle=wake["policy"]["lifecycle"],
)
inventory = {
    "host_id": "host/watch18",
    "repo_root": str(ROOT),
    "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
    "runtimes": [{
        "runtime": "codex",
        "provider": "openai",
        "lanes": ["WATCH"],
        "capabilities": [],
        "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
    }],
}

saved_base = os.environ.get("PM_BASE")
os.environ["PM_BASE"] = "https://plan.example.test"
try:
    command, mode = agent_host.launch_command(
        wake, inventory, runner_session_id="run_watch18",
        workspace_path=str(ROOT))
finally:
    if saved_base is None:
        os.environ.pop("PM_BASE", None)
    else:
        os.environ["PM_BASE"] = saved_base

child = command[command.index("--") + 1:]
ok(mode == "connect", "WATCH-18 wake resolves to connect")
ok(child[0] == "codex" and "exec" not in child,
   "interactive Codex CLI is preserved")
ok(f"mcp_servers.taikun_plan.url={json.dumps('https://plan.example.test/mcp')}"
   in child
   and 'mcp_servers.taikun_plan.bearer_token_env_var="SWITCHBOARD_CONNECT_SESSION_TOKEN"'
   in child
   and "mcp_servers.taikun_plan.required=true" in child,
   "host injects required taikun_plan MCP overrides for Connect Codex")
ok(any("Do WATCH-18 in project switchboard via Switchboard." in part
       for part in child if isinstance(part, str)),
   "host argv carries the via-Switchboard note")

print(f"\nWATCH-18 Connect via Switchboard: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
