#!/usr/bin/env python3
"""Claude Code SessionStart hook — Switchboard Tier-2 adapter (ADR-0004).

Runs deterministically at session start (the harness runs it, not the model). It:
  1. fetches the live working agreement (get_working_agreement / REST) for the project,
     falling back to the bundled docs/WORKING-AGREEMENT.md when the endpoint is unavailable;
  2. best-effort registers this session as an agent (so it shows in list_active_agents);
  3. injects the agreement into the conversation as first-turn context — so the agent starts
     in-contract without being relied on to remember the handshake.

Fail-open: any network/parse error still injects the bundled agreement; a session is never
blocked by this hook. Config via env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN.
"""
import json
import os
import sys
import urllib.request

PM_BASE = os.environ.get("PM_BASE", "https://plan.taikunai.com").rstrip("/")
PROJECT = os.environ.get("PM_PROJECT", "helm")
TOKEN = os.environ.get("PM_MCP_TOKEN", "")
TIMEOUT = 4


def _read_event():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _http(method, path, body=None):
    url = f"{PM_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _fallback_agreement():
    # bundled copy: <repo>/docs/WORKING-AGREEMENT.md (this file is adapters/claude-code/*)
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "..", "docs", "WORKING-AGREEMENT.md"))
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ("Working agreement unavailable. Core rule: you MUST NOT set a task to 'Done' "
                "— move only to 'In Review'; the merge webhook marks Done. Push before you "
                "claim progress. Main writes via PR only.")


def _live_agreement():
    # FR-23 endpoint (REST mirror of get_working_agreement); may not exist yet → caller falls back.
    return _http("GET", f"/ixp/working_agreement?project={PROJECT}")


def _agent_id(event):
    # stable id: prefer the git branch, else the session id.
    import subprocess
    try:
        b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=3,
                            cwd=event.get("cwd") or None)
        if b.returncode == 0 and b.stdout.strip():
            return f"claude/{b.stdout.strip()}"
    except Exception:
        pass
    return f"claude/{event.get('session_id', 'session')[:12]}"


def main():
    event = _read_event()
    agent_id = _agent_id(event)

    # 1. live agreement, else bundled fallback
    try:
        live = _live_agreement()
        agreement = live.get("text") or json.dumps(live, indent=2)
        source = "live (get_working_agreement)"
    except Exception:
        agreement = _fallback_agreement()
        source = "bundled fallback (live endpoint unavailable)"

    # 2. best-effort registration (don't fail the session if it's not wired yet)
    reg = "registration skipped"
    try:
        _http("POST", "/ixp/register_agent",
              {"project": PROJECT, "agent_id": agent_id,
               "session_id": event.get("session_id", ""), "fidelity": "irq"})
        reg = f"registered as {agent_id}"
    except Exception:
        reg = f"registration unavailable (would be {agent_id})"

    context = (
        f"## Switchboard working agreement — project '{PROJECT}'  [{source}; {reg}]\n\n"
        f"{agreement}\n\n"
        f"_This hook enforces the hard rule at the tool boundary: attempts to set a task to "
        f"'Done' are denied (only the merge webhook may). Move tasks to 'In Review' via "
        f"complete(evidence)._"
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context,
    }}))


if __name__ == "__main__":
    main()
