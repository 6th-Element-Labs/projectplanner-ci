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
SUPPORTED_PROTOCOL = {
    "name": "switchboard-adapter",
    "version": "ixp.v1",
    "profile": "p0-dogfood",
    "profiles": {
        "ixp_core": "1.0",
        "txp_dispatch": "0.1",
        "oxp_tally": "0.1",
        "reconcile": "0.1",
    },
}

DONE_RULE = ("Working agreement: agents do not mark tasks Done. Use "
             "complete_claim(evidence={branch, head_sha, pr_url, verification}) to move work "
             "to In Review; GitHub/default-branch provenance marks Done after the work is "
             "merged or rebased into the intended branch.")


def _requests_done(tool_input):
    ti = tool_input or {}
    vals = [ti.get("status"), ti.get("final_status")]
    ev = ti.get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = {}
    if isinstance(ev, dict):
        vals.extend([ev.get("status"), ev.get("final_status"), ev.get("done")])
    for val in vals:
        if isinstance(val, bool):
            if val:
                return True
        elif str(val or "").strip().lower() == "done":
            return True
    return False


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


def ensure_compatible(agreement):
    """Fail closed when the server advertises an unsupported protocol version."""
    if not agreement:
        return
    proto = agreement.get("protocol") or {}
    version = proto.get("version") or proto.get("ixp_version")
    compatible = proto.get("compatible_versions") or [version]
    if not version:
        raise RuntimeError("Switchboard server did not advertise a protocol version")
    if SUPPORTED_PROTOCOL["version"] not in compatible:
        raise RuntimeError(
            f"Switchboard protocol mismatch: adapter supports {SUPPORTED_PROTOCOL['version']}, "
            f"server advertises {version} compatible={compatible}"
        )


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
        ensure_compatible(agreement)
    except Exception:
        if agreement:
            raise
        agreement = None
    try:
        _http("POST", "/ixp/v1/register_agent",
              {"project": project, "agent_id": agent_id, "runtime": runtime,
               "model": model, "lane": lane, "control": control,
               "protocol": SUPPORTED_PROTOCOL}, base=base, token=token)
    except Exception:
        pass
    return agreement


def _consume_interrupt(project, me, base, token):
    try:
        q = urllib.parse.quote(me, safe="")
        r = _http("GET", f"/ixp/v1/inbox?project={project}&to_agent={q}&unacked=true", base=base, token=token)
        for m in (r.get("messages") or []):
            if m.get("signal") in ("stop", "redirect", "claim_revoked"):
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

    # 2. Definition of Done — no agent-set Done through status flips or complete_claim.
    if tool_name.endswith("update_task") and _requests_done(ti):
        return {"decision": "deny", "reason": DONE_RULE}
    if tool_name.endswith("complete_claim") and _requests_done(ti):
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


# ---- TXP dispatch helpers + the self-driving session loop (autonomy) --------------------
def heartbeat(project, agent_id, base=None, token=None):
    try:
        _http("POST", "/ixp/v1/heartbeat", {"project": project, "agent_id": agent_id}, base=base, token=token)
    except Exception:
        pass  # fail-open: a missed heartbeat just lets presence lapse


def inbox(project, agent_id, base=None, token=None):
    """Return unacked directed messages for this agent id."""
    q = urllib.parse.quote(agent_id, safe="")
    r = _http("GET", f"/ixp/v1/inbox?project={project}&to_agent={q}&unacked=true",
              base=base, token=token)
    return r.get("messages") or []


def ack(project, message_id, response="", base=None, token=None):
    """Acknowledge directed-message receipt."""
    return _http("POST", "/ixp/v1/ack",
                 {"project": project, "message_id": message_id, "response": response},
                 base=base, token=token)


def claim_next(project, agent_id, lanes=None, base=None, token=None, idem_key=""):
    body = {"project": project, "agent_id": agent_id}
    if lanes:
        body["lanes"] = lanes if isinstance(lanes, list) else [x.strip() for x in lanes.split(",") if x.strip()]
    if idem_key:
        body["idem_key"] = idem_key
    return _http("POST", "/txp/v1/claim_next", body, base=base, token=token)


def claim_task(project, task_id, agent_id, base=None, token=None,
               ttl_seconds=1800, idem_key=""):
    body = {
        "project": project,
        "task_id": task_id,
        "agent_id": agent_id,
        "ttl_seconds": ttl_seconds,
    }
    if idem_key:
        body["idem_key"] = idem_key
    return _http("POST", "/txp/v1/claim_task", body, base=base, token=token)


def complete_claim(project, claim_id, evidence, base=None, token=None, final_status=""):
    ev = evidence if isinstance(evidence, str) else __import__("json").dumps(evidence or {})
    body = {"project": project, "claim_id": claim_id, "evidence": ev}
    if final_status:
        body["final_status"] = final_status
    return _http("POST", "/txp/v1/complete_claim",
                 body, base=base, token=token)


def abandon_claim(project, claim_id, reason, base=None, token=None):
    try:
        return _http("POST", "/txp/v1/abandon_claim",
                     {"project": project, "claim_id": claim_id, "reason": reason}, base=base, token=token)
    except Exception:
        return None


def run_session(project, agent_id, runtime, work_fn, lanes=None, base=None, token=None,
                max_tasks=10, register=True):
    """Runtime-agnostic self-driving agent loop (ADR-0004 autonomy split, decision #4).

    handshake(register) → inbox(read) → repeatedly: heartbeat → claim_next → if work,
    work_fn(task)→evidence → complete_claim; else stop. Returns a summary. work_fn(task_dict)
    MUST return an evidence dict {branch, head_sha, pr_number?} (or raise to abandon the
    claim). The runtime supplies work_fn (its model actually does the task); this driver only
    orchestrates the loop. A process SUPERVISOR (Codex's lane) spawns/keeps-alive one such
    loop per agent.

    Stops on: no_unblocked_work, work_fn error (claim abandoned), or max_tasks. Fail-open on
    transport: a failed claim_next ends the loop cleanly rather than spinning.
    """
    lane_list = (lanes if isinstance(lanes, list) else
                 [x.strip() for x in (lanes or "").split(",") if x.strip()]) or None
    startup_inbox = []
    if register:
        handshake(project, agent_id, runtime, base=base, token=token,
                  lane=(lane_list[0] if lane_list else ""))
        try:
            startup_inbox = inbox(project, agent_id, base=base, token=token)
        except Exception:
            startup_inbox = []
    completed = []
    for _ in range(max(1, max_tasks)):
        heartbeat(project, agent_id, base=base, token=token)
        try:
            res = claim_next(project, agent_id, lanes=lane_list, base=base, token=token)
        except Exception as e:
            return {"completed": completed, "stopped": f"claim_error:{e}",
                    "startup_inbox": startup_inbox}
        if not res.get("claimed"):
            return {"completed": completed, "stopped": res.get("reason", "no_unblocked_work"),
                    "startup_inbox": startup_inbox}
        claim_id = res.get("claim_id") or res.get("id")
        # claim_next nests the task: claim_id is top-level, but the task id is under
        # res["task"]["task_id"] / res["names"][0] (NOT res["task_id"]). Read it robustly so
        # work_fn knows what it claimed. (Found via the live ignition test — task_id came back None.)
        task = res.get("task") or {}
        task_id = (res.get("task_id") or task.get("task_id")
                   or (res.get("names") or [None])[0])
        try:
            evidence = work_fn({**res, "task_id": task_id, "task": task}) or {}
        except Exception as e:
            abandon_claim(project, claim_id, f"work_fn error: {e}", base=base, token=token)
            return {"completed": completed, "stopped": f"work_error:{task_id}:{e}",
                    "startup_inbox": startup_inbox}
        complete_claim(project, claim_id, evidence, base=base, token=token)
        completed.append({"task_id": task_id, "evidence": evidence})
    return {"completed": completed, "stopped": "max_tasks", "startup_inbox": startup_inbox}
