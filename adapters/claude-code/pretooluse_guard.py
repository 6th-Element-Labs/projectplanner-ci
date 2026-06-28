#!/usr/bin/env python3
"""Claude Code PreToolUse hook — Switchboard Tier-2 enforcement (ADR-0004).

Runs before each matched tool call (the harness runs it, not the model) and DENIES contract
violations at the boundary — the agent self-corrects from the denial reason. Enforced rules:

  1. Agents MUST NOT self-set a task to 'Done' (only the merge webhook may). Covers the MCP
     tool `mcp__taikun-plan__update_task` and the obvious Bash/curl PATCH back-channel.
  2. (soft) write-before-claim: a file mutation (Edit/Write) with no active lease is allowed
     but flagged, since a hard check is a board round-trip per file (ADR-0004 open question).

Output contract: print a PreToolUse decision JSON. No output + exit 0 = allow silently.
"""
import json
import re
import sys


def _event():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def _allow_with_note(note):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": note,
    }}))
    sys.exit(0)


DONE_RULE = ("Working agreement (ADR-0003): agents do not set a task to 'Done'. Move it to "
             "'In Review' via complete(task_id, agent_id, evidence={branch, head_sha, pr}); "
             "the merge webhook marks 'Done' when the PR merges. Re-issue with status='In Review'.")


def main():
    ev = _event()
    name = ev.get("tool_name", "")
    ti = ev.get("tool_input", {}) or {}

    # Rule 1a: the MCP board write tool setting status=Done
    if name.endswith("update_task") and str(ti.get("status", "")).strip().lower() == "done":
        _deny(DONE_RULE)

    # Rule 1b: the Bash/curl back-channel (PATCH /api/tasks ... "status":"Done", or a CLI that does)
    if name == "Bash":
        cmd = ti.get("command", "") or ""
        if re.search(r"status['\"]?\s*[:=]\s*['\"]?done", cmd, re.I) and \
           re.search(r"/api/tasks/|update_task|tasks/.*\b(PATCH|patch)\b|curl", cmd):
            _deny(DONE_RULE + "  (Detected a Bash back-channel attempt to set Done.)")

    # Rule 2 (soft): file mutation — remind to claim first. Allowed, not blocked (see ADR-0004).
    if name in ("Edit", "Write", "NotebookEdit"):
        _allow_with_note("Reminder: claim the file(s) (claim_files) before editing so other "
                         "agents see the lease, and push your branch before you claim progress.")

    # everything else: allow silently
    sys.exit(0)


if __name__ == "__main__":
    main()
