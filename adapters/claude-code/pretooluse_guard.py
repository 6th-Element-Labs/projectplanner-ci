#!/usr/bin/env python3
"""Claude Code PreToolUse hook — Switchboard Tier-2 enforcement (ADR-0004 / ENFORCE-2).

Runs before each matched tool call (the harness runs it, not the model) and DENIES contract
violations at the boundary — the agent self-corrects from the denial reason. Rules:

  1. Agents MUST NOT self-set a task to 'Done' (only the merge webhook may). Covers the MCP
     `update_task` tool and the obvious Bash/curl PATCH back-channel.
  2. A file edit (Edit/Write/NotebookEdit) on a resource ANOTHER agent holds a lease on is
     DENIED — checked live against Codex's /ixp/v1/check registry. Self-held = allowed;
     unheld = allowed with a claim-first reminder. On a deny it sends the holder a heads-up
     (best-effort) so the event is recorded. This is the ENFORCE-2 hook-deny prototype.

Fail-open: any board/network error ALLOWS the call — the hook never bricks an edit. Config via
env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID (must match what SessionStart registered).
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

PM_BASE = os.environ.get("PM_BASE", "https://plan.taikunai.com").rstrip("/")
PROJECT = os.environ.get("PM_PROJECT", "helm")
TOKEN = os.environ.get("PM_MCP_TOKEN", "")
TIMEOUT = 4


def _event():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{PM_BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _agent_id(cwd):
    if os.environ.get("PM_AGENT_ID"):
        return os.environ["PM_AGENT_ID"]
    try:
        b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=3, cwd=cwd or None)
        if b.returncode == 0 and b.stdout.strip():
            return f"claude/{b.stdout.strip()}"
    except Exception:
        pass
    return "claude-code"


def _repo_rel(path, cwd):
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    if not root:
        try:
            t = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=3, cwd=cwd or None)
            root = t.stdout.strip() if t.returncode == 0 else (cwd or os.getcwd())
        except Exception:
            root = cwd or os.getcwd()
    try:
        return os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
    except Exception:
        return path


def _emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


DONE_RULE = ("Working agreement (ADR-0003): agents do not set a task to 'Done'. Move it to "
             "'In Review' via complete(task_id, agent_id, evidence={branch, head_sha, pr}); "
             "the merge webhook marks 'Done' when the PR merges. Re-issue with status='In Review'.")


def _lease_holder(relpath):
    try:
        r = _http("POST", "/ixp/v1/check", {"project": PROJECT, "names": [relpath]})
        for h in (r.get("held") or []):
            if h.get("name") == relpath:
                return h
    except Exception:
        return None  # fail-open: board unreachable → no holder known → allow
    return None


def main():
    ev = _event()
    name = ev.get("tool_name", "")
    ti = ev.get("tool_input", {}) or {}
    cwd = ev.get("cwd") or os.getcwd()

    # Rule 1a: MCP board write tool setting status=Done
    if name.endswith("update_task") and str(ti.get("status", "")).strip().lower() == "done":
        _emit("deny", DONE_RULE)
    # Rule 1b: Bash/curl back-channel to set Done
    if name == "Bash":
        cmd = ti.get("command", "") or ""
        if re.search(r"status['\"]?\s*[:=]\s*['\"]?done", cmd, re.I) and \
           re.search(r"/api/tasks/|update_task|/txp/|curl", cmd):
            _emit("deny", DONE_RULE + "  (Detected a Bash back-channel attempt to set Done.)")

    # Rule 2: file edit on a resource another agent holds
    if name in ("Edit", "Write", "NotebookEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if not path:
            sys.exit(0)
        rel = _repo_rel(path, cwd)
        me = _agent_id(cwd)
        holder = _lease_holder(rel)
        if holder and holder.get("held_by") and holder["held_by"] != me:
            try:  # best-effort heads-up to the lease holder (records the event)
                _http("POST", "/ixp/v1/send", {
                    "project": PROJECT, "from_agent": me, "to_agent": holder["held_by"],
                    "task": holder.get("task_id"), "signal": "heads_up",
                    "message": f"{me} was denied an edit to {rel} — your active lease "
                               f"(task {holder.get('task_id')}). Release it if you're done."})
            except Exception:
                pass
            _emit("deny", f"'{rel}' is leased by {holder['held_by']} (task {holder.get('task_id')}). "
                          f"Don't edit another agent's file — coordinate on the board, wait for "
                          f"release, or claim it yourself once free. (ENFORCE-2)")
        if not holder:
            _emit("allow", "Reminder: claim this file (/ixp/v1/claim) before editing so other "
                           "agents see your lease, and push your branch before claiming progress.")
        # held by me → allow silently
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
