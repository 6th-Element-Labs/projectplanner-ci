#!/usr/bin/env python3
"""Runtime-agnostic Switchboard adapter core (ADR-0004).

The coordination logic lives here ONCE; each runtime's adapter (Claude Code, Codex, Cursor,
LangGraph) only maps its own hook I/O to/from these calls. That keeps every adapter bound to
the same contract instead of reverse-engineering it per runtime (the drift that bit the Claude
adapter on /ixp vs /ixp/v1).

Two entry points:
  handshake(project, agent_id, runtime, ...)            -> agreement text  (call at session start)
  evaluate_tool(project, agent_id, tool_name, tool_input, cwd) -> {"decision","reason"}

evaluate_tool applies, in priority order:
  1. FR-14 interrupt-consume — an inbound stop/redirect signal addressed to me denies the
     pending tool (and is acked, consume-once).
  2. Definition-of-Done — deny an agent setting a task to 'Done' (MCP update_task + Bash back-channel).
  3. Lease conflict — deny editing a file another agent holds a lease on (+ heads-up to holder).

Fail-open: any board/network error returns allow — never brick a tool call. Config via args or
env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID.
"""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

DEFAULT_BASE = os.environ.get("PM_BASE", "https://plan.taikunai.com").rstrip("/")
TIMEOUT = 4

DONE_RULE = ("Working agreement (ADR-0003): agents do not set a task to 'Done'. Move it to "
             "'In Review' via complete(task_id, agent_id, evidence={branch, head_sha, pr}); the "
             "merge webhook marks 'Done' on PR merge. Re-issue with status='In Review'.")


def _http(method, path, body=None, base=None, token=None):
    base = (base or DEFAULT_BASE).rstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    token = token if token is not None else os.environ.get("PM_MCP_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def agent_id(cwd=None):
    if os.environ.get("PM_AGENT_ID"):
        return os.environ["PM_AGENT_ID"]
    try:
        b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=3, cwd=cwd or None)
        if b.returncode == 0 and b.stdout.strip():
            return f"claude/{b.stdout.strip()}"
    except Exception:
        pass
    return "agent"


def repo_rel(path, cwd=None):
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


def handshake(project, agent_id, runtime, base=None, token=None, model="", lane="",
              control=None):
    """Session-start: fetch the working agreement (live, fallback to None) + register. Returns
    the agreement (dict or None). The runtime adapter surfaces it as first-turn context."""
    control = control or {"interrupt": "tool_boundary", "deny": "pre_tool", "kill": "runner"}
    agreement = None
    try:
        agreement = _http("GET", f"/ixp/v1/working_agreement?project={project}", base=base, token=token)
    except Exception:
        agreement = None
    try:
        _http("POST", "/ixp/v1/register_agent",
              {"project": project, "agent_id": agent_id, "runtime": runtime,
               "model": model, "lane": lane, "control": control}, base=base, token=token)
    except Exception:
        pass
    return agreement


def _consume_interrupt(project, me, base, token):
    try:
        q = urllib.parse.quote(me, safe="")
        r = _http("GET", f"/ixp/v1/inbox?project={project}&to_agent={q}&unacked=true", base=base, token=token)
        for m in (r.get("messages") or []):
            if m.get("signal") in ("stop", "redirect"):
                try:
                    _http("POST", "/ixp/v1/ack",
                          {"project": project, "message_id": m.get("id"),
                           "response": "consumed at tool boundary"}, base=base, token=token)
                except Exception:
                    pass
                return m["signal"], m.get("message") or "", m.get("from_agent") or "?"
    except Exception:
        return None
    return None


def _lease_holder(project, relpath, base, token):
    try:
        r = _http("POST", "/ixp/v1/check", {"project": project, "names": [relpath]}, base=base, token=token)
        for h in (r.get("held") or []):
            if h.get("name") == relpath:
                return h
    except Exception:
        return None
    return None


def evaluate_tool(project, me, tool_name, tool_input, cwd=None, base=None, token=None):
    """Return {"decision": "allow"|"deny", "reason": str} for one pending tool call.
    Runtime-agnostic: the adapter normalizes its hook payload into (tool_name, tool_input) and
    maps the returned decision onto its own deny mechanism. Fail-open."""
    ti = tool_input or {}

    # 1. FR-14 interrupt-consume (highest priority — preempts everything)
    intr = _consume_interrupt(project, me, base, token)
    if intr:
        sig, msg, frm = intr
        return {"decision": "deny",
                "reason": f"[{sig.upper()} from {frm}] {msg}  — interrupt consumed at the tool "
                          f"boundary (FR-14). Halt or redirect before any further tool use."}

    # 2. Definition of Done — no agent self-set
    if tool_name.endswith("update_task") and str(ti.get("status", "")).strip().lower() == "done":
        return {"decision": "deny", "reason": DONE_RULE}
    if tool_name == "Bash":
        cmd = ti.get("command", "") or ""
        if re.search(r"status['\"]?\s*[:=]\s*['\"]?done", cmd, re.I) and \
           re.search(r"/api/tasks/|update_task|/txp/|curl", cmd):
            return {"decision": "deny", "reason": DONE_RULE + "  (Bash back-channel to set Done.)"}

    # 3. Lease conflict — don't edit another agent's leased file
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if not path:
            return {"decision": "allow", "reason": ""}
        rel = repo_rel(path, cwd)
        holder = _lease_holder(project, rel, base, token)
        if holder and holder.get("held_by") and holder["held_by"] != me:
            try:  # heads-up to the holder (records the event)
                _http("POST", "/ixp/v1/send",
                      {"project": project, "from_agent": me, "to_agent": holder["held_by"],
                       "task": holder.get("task_id"), "signal": "heads_up",
                       "message": f"{me} was denied an edit to {rel} — your active lease "
                                  f"(task {holder.get('task_id')})."}, base=base, token=token)
            except Exception:
                pass
            return {"decision": "deny",
                    "reason": f"'{rel}' is leased by {holder['held_by']} (task {holder.get('task_id')}). "
                              f"Coordinate on the board, wait for release, or claim it once free."}
        if not holder:
            return {"decision": "allow",
                    "reason": "Reminder: claim this file (/ixp/v1/claim) before editing so peers see your lease."}

    return {"decision": "allow", "reason": ""}
