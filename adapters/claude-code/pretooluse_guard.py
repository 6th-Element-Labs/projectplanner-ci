#!/usr/bin/env python3
"""Claude Code PreToolUse hook — THIN SHIM over adapters/switchboard_core.py (ADR-0004).

All enforcement logic (FR-14 interrupt-consume, self-Done deny, lease-conflict deny) lives in
the shared core's evaluate_tool(), so Claude Code and Codex provably run the *same* contract.
This file only maps Claude Code's PreToolUse I/O: stdin tool-call JSON → core verdict →
permissionDecision. Fail-open (the core returns allow on any board/network error).

Config via env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import switchboard_core as sb  # noqa: E402

PROJECT = os.environ.get("PM_PROJECT", "helm")


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    cwd = event.get("cwd") or os.getcwd()
    me = sb.agent_id(cwd)

    verdict = sb.evaluate_tool(PROJECT, me, tool_name, tool_input, cwd=cwd)
    decision = verdict.get("decision", "allow")
    reason = verdict.get("reason", "")

    if decision == "deny":
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    elif reason:  # allow + soft reminder (e.g. "claim before editing")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }}))
    # allow with no reason → exit 0 silently (permit)


if __name__ == "__main__":
    main()
