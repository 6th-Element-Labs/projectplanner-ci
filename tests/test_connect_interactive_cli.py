#!/usr/bin/env python3
"""Operator Connect stays interactive; Mission Bot Codex is one-shot.

Operator-launched sessions retain the real CLI composer. Mission Bot Codex
assignments carry a durable lifecycle mission_key and run as ``codex exec`` so
the supervised child exits naturally after its final response. Capacity remains
the only process-lifecycle authority in both cases.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from execution_policy_fixture import ready_execution_context
from switchboard.application.commands.execution_context import with_generation
from switchboard.connect.execution_assignment import build_execution_assignment


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def connect_wake(runtime, *, mission_key="", project="switchboard"):
    wake = {
        "wake_id": "wake-interactive",
        "task_id": "WATCH-10",
        "_host_project": project,
        "selector": {"runtime": runtime, "task_id": "WATCH-10",
                     "agent_id": f"agent/{runtime}/watch-10"},
        "policy": {"mode": "connect", "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            "assignment_id": "assignment-interactive",
            "principal_ref": f"agent/{runtime}/watch-10",
            "work_ref": f"task:{project}:WATCH-10",
            "runtime": runtime,
            "provider": {"codex": "openai", "claude-code": "anthropic",
                         "cursor": "cursor"}[runtime],
            "workspace_ref": "repo:canonical",
            "queued_at": 1.0,
            "limits": {"max_runtime_seconds": 7200,
                       "spend_limit_microunits": 0, "memory_limit_bytes": 0},
        }, "lifecycle": {
            "schema": "switchboard.execution_lifecycle.v1",
            "role": "implementation", "head_sha": "",
            "pr_number": 0, "pr_url": "", "ttl_seconds": 7200,
            "execution_id": f"execlease-interactive-{runtime}",
            "generation": 1, "fence_epoch": 1,
        }},
    }
    if mission_key:
        wake["policy"]["lifecycle"]["mission_key"] = mission_key
    wake["policy"]["execution_context"] = with_generation(
        ready_execution_context("WATCH-10", runtime=runtime), 1)
    wake["policy"]["execution_assignment"] = build_execution_assignment(
        task_id=wake["task_id"],
        assignment=wake["policy"]["assignment"],
        lifecycle=wake["policy"]["lifecycle"],
        execution_context=wake["policy"]["execution_context"],
    )
    return wake


inventory = {"host_id": "host/interactive-test", "repo_root": str(ROOT),
             "policy": {"allow_global_claim": False, "allow_work": True,
                        "lane_mode": "all_project_lanes"},
             "runtimes": [
                 {"runtime": rt, "provider": prov, "lanes": [],
                  "policy": {"allow_work": True, "lane_mode": "all_project_lanes"}}
                 for rt, prov in (("codex", "openai"), ("claude-code", "anthropic"),
                                  ("cursor", "cursor"))
             ]}

cmd, mode = agent_host.launch_command(
    connect_wake("codex"), inventory, runner_session_id="run_interactive",
    workspace_path=str(ROOT))
child = cmd[cmd.index("--") + 1:]
ok(mode == "connect", "wake resolves to connect mode")
ok(child[0] == "codex", f"codex launches the codex CLI (got {child[0]})")
ok("exec" not in child,
   "codex launches the INTERACTIVE TUI, not one-shot `codex exec` "
   f"(argv={child[:3]})")
ok("--dangerously-bypass-approvals-and-sandbox" in child,
   "codex keeps the sandbox/approval bypass for autonomous work")
ok("mcp_servers.taikun_plan.required=true" in child
   and any("Do WATCH-10 in project switchboard via Switchboard." in part
           for part in child if isinstance(part, str)),
   "codex Connect boots with required MCP and the via-Switchboard note")

locked_cmd, _ = agent_host.launch_command(
    connect_wake("codex"), inventory, runner_session_id="run_locked_runtime",
    workspace_path=str(ROOT), verification_runtime={
        "schema": "switchboard.host_python_runtime.v1",
    })
locked_child = locked_cmd[locked_cmd.index("--") + 1:]
ok("allow_login_shell=false" in locked_child,
   "a Host-proven runtime reaches Codex with login-shell PATH rewriting disabled")

mission_cmd, mission_mode = agent_host.launch_command(
    connect_wake("codex", mission_key="mission-bot:WATCH-10:1"),
    inventory, runner_session_id="run_mission_one_shot",
    workspace_path=str(ROOT))
mission_child = mission_cmd[mission_cmd.index("--") + 1:]
ok(mission_mode == "connect" and mission_child[:2] == ["codex", "exec"],
   "Mission Bot Codex launches one-shot `codex exec` "
   f"(argv={mission_child[:3]})")
ok("--dangerously-bypass-approvals-and-sandbox" in mission_child
   and "mcp_servers.taikun_plan.required=true" in mission_child
   and not any(str(value).startswith("model_reasoning_effort=")
               for value in mission_child),
   "Mission Bot Codex preserves autonomous permissions and required MCP without "
   "inventing a model profile")

simplemark_cmd, _ = agent_host.launch_command(
    connect_wake("codex", mission_key="mission-bot:WATCH-10:2",
                 project="simplemark"),
    inventory, runner_session_id="run_simplemark",
    workspace_path=str(ROOT))
simplemark_child = simplemark_cmd[simplemark_cmd.index("--") + 1:]
ok(any("mcp?project=simplemark" in part for part in simplemark_child),
   "Connect binds the child MCP endpoint to the wake project, not PM_PROJECT")

claude_cmd, _ = agent_host.launch_command(
    connect_wake("claude-code"), inventory, runner_session_id="run_interactive2",
    workspace_path=str(ROOT))
claude_child = claude_cmd[claude_cmd.index("--") + 1:]
ok(claude_child[0] == "claude" and "-p" not in claude_child,
   "claude-code launches the interactive CLI, not print mode "
   f"(argv={claude_child[:3]})")

cursor_cmd, _ = agent_host.launch_command(
    connect_wake("cursor"), inventory, runner_session_id="run_interactive3",
    workspace_path=str(ROOT))
cursor_child = cursor_cmd[cursor_cmd.index("--") + 1:]
ok(cursor_child[0] == "cursor-agent" and "-p" not in cursor_child,
   "cursor launches the interactive CLI, not print mode "
   f"(argv={cursor_child[:3]})")

print(f"\nConnect interactive CLI: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
