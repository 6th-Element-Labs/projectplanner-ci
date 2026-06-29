#!/usr/bin/env python3
"""Claude Code SessionStart hook — THIN SHIM over adapters/switchboard_core.py (ADR-0004).

All handshake logic (fetch working agreement + register) lives in the shared core, so Claude
Code and Codex provably run the *same* contract. This file only maps Claude Code's SessionStart
I/O: stdin event JSON in, `additionalContext` out. Fail-open (the core never raises; a missing
live agreement falls back to the bundled copy here for presentation).

Config via env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID, PM_LANE, PM_AGENT_MODEL.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import switchboard_core as sb  # noqa: E402

PROJECT = os.environ.get("PM_PROJECT", "helm")


def _bundled_agreement():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "..", "docs", "WORKING-AGREEMENT.md"))
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ("Working agreement unavailable. Core rule: use complete_claim(evidence=...) "
                "to move work to In Review; Done requires GitHub/default-branch merge provenance.")


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    cwd = event.get("cwd") or os.getcwd()
    me = sb.agent_id(cwd)

    agreement = sb.handshake(PROJECT, me, "claude-code",
                             lane=os.environ.get("PM_LANE", ""),
                             model=os.environ.get("PM_AGENT_MODEL", ""))
    if isinstance(agreement, dict):
        text = agreement.get("text") or json.dumps(agreement, indent=2, sort_keys=True)
        src = "live (get_working_agreement)"
    else:
        text = _bundled_agreement()
        src = "bundled fallback (live endpoint unavailable)"

    context = (
        f"## Switchboard working agreement — project '{PROJECT}'  [{src}; registered as {me}]\n\n"
        f"{text}\n\n"
        f"_This session is governed at the tool boundary by the PreToolUse hook "
        f"(adapters/switchboard_core.evaluate_tool): inbound stop/redirect interrupts are "
        f"consumed, and naked Done + lease-conflict edits are denied._"
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context,
    }}))


if __name__ == "__main__":
    main()
