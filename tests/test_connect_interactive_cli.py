#!/usr/bin/env python3
"""Connect boots the INTERACTIVE CLI; Switchboard assigns the task.

The operator contract: a Connect launch starts the real interactive CLI session
(Codex TUI / Claude Code / cursor-agent) inside the supervised PTY, and the agent
receives its task through the Switchboard handshake. Batch one-shot modes
(`codex exec`, `claude -p`, `cursor-agent -p`) produce a scrolling log with no
composer -- Watch renders a wall of text and "Message the live agent" can inject
into nothing. That is what shipped in the DISPATCH-12 cutover and it broke the
operator window's whole premise.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from adapters import agent_host


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def connect_wake(runtime):
    return {
        "wake_id": "wake-interactive",
        "task_id": "WATCH-10",
        "selector": {"runtime": runtime, "task_id": "WATCH-10",
                     "agent_id": f"agent/{runtime}/watch-10"},
        "policy": {"mode": "connect", "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            "assignment_id": "assignment-interactive",
            "principal_ref": f"agent/{runtime}/watch-10",
            "work_ref": "task:switchboard:WATCH-10",
            "runtime": runtime,
            "provider": {"codex": "openai", "claude-code": "anthropic",
                         "cursor": "cursor"}[runtime],
            "workspace_ref": "repo:canonical",
            "queued_at": 1.0,
            "limits": {"max_runtime_seconds": 7200,
                       "spend_limit_microunits": 0, "memory_limit_bytes": 0},
        }},
    }


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
    connect_wake("codex"), inventory, runner_session_id="run_interactive")
child = cmd[cmd.index("--") + 1:]
ok(mode == "connect", "wake resolves to connect mode")
ok(child[0] == "codex", f"codex launches the codex CLI (got {child[0]})")
ok("exec" not in child,
   "codex launches the INTERACTIVE TUI, not one-shot `codex exec` "
   f"(argv={child[:3]})")
ok("--dangerously-bypass-approvals-and-sandbox" in child,
   "codex keeps the sandbox/approval bypass for autonomous work")

claude_cmd, _ = agent_host.launch_command(
    connect_wake("claude-code"), inventory, runner_session_id="run_interactive2")
claude_child = claude_cmd[claude_cmd.index("--") + 1:]
ok(claude_child[0] == "claude" and "-p" not in claude_child,
   "claude-code launches the interactive CLI, not print mode "
   f"(argv={claude_child[:3]})")

cursor_cmd, _ = agent_host.launch_command(
    connect_wake("cursor"), inventory, runner_session_id="run_interactive3")
cursor_child = cursor_cmd[cursor_cmd.index("--") + 1:]
ok(cursor_child[0] == "cursor-agent" and "-p" not in cursor_child,
   "cursor launches the interactive CLI, not print mode "
   f"(argv={cursor_child[:3]})")

print(f"\nConnect interactive CLI: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
