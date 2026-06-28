#!/usr/bin/env python3
"""Codex adapter — Switchboard Tier-2 (ADR-0004 / ADAPTER-2).

SKELETON authored by claude-code (decision #2): the coordination logic is done and proven in
adapters/switchboard_core.py. This file shows where to wire it to Codex's runtime. The two
`TODO(codex)` blocks are the ONLY Codex-runtime-specific parts — only Codex knows its own hook
lifecycle + how its pre-tool hook receives the pending call and signals a deny.

Contract (must do, per ADR-0004):
  1. On session start: surface the working agreement as first-turn context + register_agent.
  2. On each tool call (if the runtime allows a pre-tool hook): deny self-Done, deny edits to a
     file another agent holds, and consume inbound stop/redirect signals (FR-14).
  3. Advertise control fidelity so the board knows how strongly this agent is governed.

Config via env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID (e.g. 'codex/<task>').
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import switchboard_core as sb  # noqa: E402

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
RUNTIME = "codex"


def on_session_start():
    """Call when a Codex session begins."""
    me = sb.agent_id()
    agreement = sb.handshake(PROJECT, me, RUNTIME, lane=os.environ.get("PM_LANE", ""))
    text = (agreement if isinstance(agreement, str) else
            (sb.json.dumps(agreement, indent=2) if agreement else "(working agreement unavailable)"))
    # TODO(codex): surface `text` to the model as first-turn/system context using your runtime's
    # mechanism (system prompt injection, a primer message, or your SessionStart-equivalent hook).
    return text


def on_pre_tool(pending):
    """Call before each tool the model wants to run.

    `pending` is whatever your runtime hands a pre-tool hook. Normalize it to (tool_name,
    tool_input, cwd), ask the shared core, then map the decision to your deny mechanism.
    """
    # TODO(codex): map your runtime's pre-tool payload → these three fields.
    tool_name = pending.get("tool_name", "")
    tool_input = pending.get("tool_input", {}) or {}
    cwd = pending.get("cwd") or os.getcwd()

    me = sb.agent_id(cwd)
    verdict = sb.evaluate_tool(PROJECT, me, tool_name, tool_input, cwd=cwd)

    # TODO(codex): translate verdict into your runtime's hook result.
    #   verdict["decision"] == "deny"  -> block the pending tool, surfacing verdict["reason"]
    #                                     to the model so it self-corrects (or halts on an interrupt).
    #   verdict["decision"] == "allow" -> permit; if verdict["reason"] is non-empty it's a
    #                                     soft reminder you MAY surface (e.g. "claim before editing").
    return verdict


if __name__ == "__main__":
    # Smoke: prints the agreement + a sample verdict. Replace with your hook wiring.
    print(on_session_start()[:200])
    print(on_pre_tool({"tool_name": "mcp__taikun-plan__update_task",
                       "tool_input": {"status": "Done"}}))
