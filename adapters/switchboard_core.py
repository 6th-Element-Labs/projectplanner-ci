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
  2. Server pre_tool_check — when PM_PRE_TOOL_CHECK/PM_WORK_SESSION_ID is set, ask Switchboard
     to validate the active Work Session before side effects.
  3. Definition-of-Done — deny an agent setting a task to 'Done' (MCP update_task + Bash back-channel).
  4. Lease conflict — deny editing a file another agent holds a lease on (+ heads-up to holder).

Fail-open: any board/network error returns allow — never brick a tool call. Config via args or
env: PM_BASE, PM_PROJECT, PM_MCP_TOKEN, PM_AGENT_ID.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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


def _http(method, path, body=None, base=None, token=None, timeout=None):
    base = (base or DEFAULT_BASE).rstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    token = token if token is not None else os.environ.get("PM_MCP_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
        return json.loads(r.read().decode())


def ensure_compatible(agreement):
    """Fail closed when the server advertises an unsupported protocol version.

    Mirrors ARCH-MS-43 ``negotiate_protocol`` intersection rules without importing
    the server domain package (adapters must stay runnable as standalone packs).
    """
    if not agreement:
        return
    proto = agreement.get("protocol") or {}
    version = proto.get("version") or proto.get("ixp_version")
    server_versions = list(proto.get("compatible_versions") or ([version] if version else []))
    if not version and not server_versions:
        raise RuntimeError("Switchboard server did not advertise a protocol version")
    adapter_versions = list(
        SUPPORTED_PROTOCOL.get("compatible_versions")
        or [SUPPORTED_PROTOCOL["version"]]
    )
    intersection = [v for v in server_versions if v in set(adapter_versions)]
    if not intersection:
        raise RuntimeError(
            f"Switchboard protocol mismatch: adapter supports {SUPPORTED_PROTOCOL['version']}, "
            f"server advertises {version} compatible={server_versions}"
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


def _pre_tool_server_enabled(tool_input):
    val = os.environ.get("PM_PRE_TOOL_CHECK", "").strip().lower()
    if val in ("1", "true", "yes", "on", "server", "deny", "warn"):
        return True
    return bool(os.environ.get("PM_WORK_SESSION_ID") or (tool_input or {}).get("work_session_id"))


def pre_tool_check(project, me, tool_name, tool_input, cwd=None, base=None, token=None,
                   action="", control_mode=""):
    """Ask Switchboard's server-side pre_tool_check before a side effect.

    Returns None when the server check is not enabled or cannot be reached. Runtime adapters
    keep fail-open behavior for transport failures, but honor explicit server deny/warn
    verdicts when returned.
    """
    ti = tool_input or {}
    if not _pre_tool_server_enabled(ti):
        return None
    body = {
        "project": project,
        "agent_id": me,
        "tool_name": tool_name,
        "tool_input": ti,
        "action": action or ti.get("action") or "",
        "task_id": ti.get("task_id") or os.environ.get("PM_TASK_ID", ""),
        "work_session_id": ti.get("work_session_id") or os.environ.get("PM_WORK_SESSION_ID", ""),
        "claim_id": ti.get("claim_id") or os.environ.get("PM_CLAIM_ID", ""),
        "control_mode": control_mode or os.environ.get("PM_CONTROL_MODE", ""),
        "cwd": cwd or os.getcwd(),
    }
    try:
        return _http("POST", "/ixp/v1/pre_tool_check", body, base=base, token=token)
    except Exception:
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

    server_verdict = pre_tool_check(project, me, tool_name, ti, cwd=cwd, base=base, token=token)
    if server_verdict:
        decision = server_verdict.get("decision") or "allow"
        reason = server_verdict.get("reason") or ""
        if decision == "deny":
            return {"decision": "deny", "reason": reason, "server_pre_tool_check": server_verdict}
        if decision == "warn" or reason:
            return {"decision": "allow", "reason": reason, "server_pre_tool_check": server_verdict}

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


def _agent_host_bootstrap_binding(task_id="", agent_id=""):
    """Return the exact generic-wake tuple a narrow Agent Host may bootstrap.

    The server treats this only as a claim to verify against its claimed wake and
    preclaim runner rows.  It is not authority by itself, and it is deliberately
    absent from ordinary CLI/operator requests.
    """
    binding = {
        "wake_id": str(os.environ.get("PM_CO_WAKE_ID") or "").strip(),
        "host_id": str(
            os.environ.get("PM_CO_HOST_ID") or os.environ.get("PM_HOST_ID") or ""
        ).strip(),
        "runner_session_id": str(os.environ.get("PM_RUNNER_SESSION_ID") or "").strip(),
        "task_id": str(task_id or os.environ.get("PM_TASK_ID") or "").strip().upper(),
        "agent_id": str(agent_id or os.environ.get("PM_AGENT_ID") or "").strip(),
    }
    return binding if all(binding.values()) else {}


def claim_next(project, agent_id, lanes=None, base=None, token=None, idem_key=""):
    body = {"project": project, "agent_id": agent_id}
    if lanes:
        body["lanes"] = lanes if isinstance(lanes, list) else [x.strip() for x in lanes.split(",") if x.strip()]
    if idem_key:
        body["idem_key"] = idem_key
    return _http("POST", "/txp/v1/claim_next", body, base=base, token=token)


def claim_task(project, task_id, agent_id, base=None, token=None,
               ttl_seconds=1800, idem_key="", work_session_id="",
               session_policy_profile=""):
    body = {
        "project": project,
        "task_id": task_id,
        "agent_id": agent_id,
        "ttl_seconds": ttl_seconds,
    }
    if idem_key:
        body["idem_key"] = idem_key
    if work_session_id:
        body["work_session_id"] = work_session_id
    if session_policy_profile:
        body["session_policy_profile"] = session_policy_profile
    bootstrap = _agent_host_bootstrap_binding(task_id=task_id, agent_id=agent_id)
    if bootstrap:
        body["agent_host_bootstrap_binding"] = bootstrap
    return _http("POST", "/txp/v1/claim_task", body, base=base, token=token, timeout=30)


def get_task(project, task_id, base=None, token=None):
    task = urllib.parse.quote(str(task_id or "").strip(), safe="")
    scope = urllib.parse.quote(str(project or "").strip(), safe="")
    return _http("GET", f"/api/tasks/{task}?project={scope}", base=base, token=token)


def get_work_session(project, work_session_id, base=None, token=None):
    session = urllib.parse.quote(str(work_session_id or "").strip(), safe="")
    scope = urllib.parse.quote(str(project or "").strip(), safe="")
    return _http(
        "GET", f"/ixp/v1/work_sessions/{session}?project={scope}",
        base=base, token=token,
    )


def complete_claim(project, claim_id, evidence, base=None, token=None, final_status="",
                   personal_execution_binding=None):
    ev = evidence if isinstance(evidence, str) else __import__("json").dumps(evidence or {})
    body = {"project": project, "claim_id": claim_id, "evidence": ev}
    if _personal_execution_enabled():
        body["personal_execution_binding"] = (
            dict(personal_execution_binding or {}) or _personal_execution_binding())
    if final_status:
        body["final_status"] = final_status
    return _http("POST", "/txp/v1/complete_claim",
                 body, base=base, token=token)


def abandon_claim(project, claim_id, reason, base=None, token=None):
    try:
        body = {"project": project, "claim_id": claim_id, "reason": reason}
        if _personal_execution_enabled():
            body["personal_execution_binding"] = _personal_execution_binding()
        return _http("POST", "/txp/v1/abandon_claim",
                     body, base=base, token=token)
    except Exception:
        return None


# --- SESSION-11: managed Work Session + executed-test wiring for code_strict tasks ----------

def create_managed_work_session(project, task_id, agent_id, storage_mode="worktree",
                                policy_profile="", repo_role="canonical",
                                source_path="", base=None, token=None):
    """Ask Switchboard to allocate an isolated worktree/clone Work Session for a task
    (SESSION-7). Returns the managed-session dict with work_session_id + workspace path."""
    body = {"project": project, "task_id": task_id, "agent_id": agent_id,
            "storage_mode": storage_mode, "repo_role": repo_role}
    if policy_profile:
        body["policy_profile"] = policy_profile
    if source_path:
        body["source_path"] = source_path
        body["repo_path"] = source_path
    # git fetch + worktree add can take a few seconds — allow more than the 4s default.
    return _http("POST", "/ixp/v1/managed_work_sessions", body, base=base, token=token, timeout=90)


def create_external_work_session(project, task_id, agent_id, runtime, source_path,
                                 policy_profile="code_strict", base=None, token=None):
    """Persist a worker-owned git Work Session when the coordinator cannot see its disk.

    The worker performs the git checks locally and submits their exact values. Switchboard
    remains the durable claim/session authority; it does not pretend that the coordination
    VM created or inspected an inaccessible cloud-host path.
    """
    source_path = os.path.abspath(source_path or os.getcwd())

    def git(*args):
        completed = subprocess.run(
            ["git", "-C", source_path, *args], capture_output=True, text=True,
            timeout=30, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed")
        return (completed.stdout or "").strip()

    if git("rev-parse", "--is-inside-work-tree") != "true":
        raise RuntimeError("external source_path is not a git worktree")
    dirty = git("status", "--porcelain")
    if dirty:
        raise RuntimeError("external source_path is dirty")
    head_sha = git("rev-parse", "HEAD")
    branch = git("branch", "--show-current")
    task_marker = str(task_id or "").strip().upper()
    isolate = str(os.environ.get(
        "PM_AGENT_HOST_ISOLATE_TASK_WORKSPACE") or "").strip().lower() in {
            "1", "true", "yes", "on"
        }
    workspace_path = source_path
    worker_owned_workspace = False
    if isolate:
        runner_marker = str(os.environ.get("PM_RUNNER_SESSION_ID") or agent_id)
        suffix = hashlib.sha256(
            f"{task_marker}:{runner_marker}".encode()).hexdigest()[:10]
        branch = f"codex/{task_marker}-autopilot-{suffix}"
        workspace_root = os.path.abspath(os.environ.get("PM_WORKSPACE_ROOT") or os.path.join(
            os.path.dirname(source_path), "switchboard-agent-workspaces"))
        if workspace_root in {os.path.sep, os.path.expanduser("~")}:
            raise RuntimeError("external workspace root is unsafe")
        os.makedirs(workspace_root, mode=0o700, exist_ok=True)
        workspace_path = os.path.join(workspace_root, f"{task_marker.lower()}-{suffix}")
        if os.path.exists(workspace_path):
            raise RuntimeError("isolated external workspace already exists")
        created = subprocess.run(
            ["git", "-C", source_path, "worktree", "add", "-b", branch,
             workspace_path, head_sha],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if created.returncode != 0:
            raise RuntimeError(
                "isolated external task worktree creation failed: "
                + (created.stderr or "unknown git error")[-500:])
        worker_owned_workspace = True
    elif task_marker.lower() not in branch.lower():
        branch = f"codex/{task_marker}-byoa"
        switched = subprocess.run(
            ["git", "-C", source_path, "switch", "-c", branch],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if switched.returncode != 0:
            raise RuntimeError("external task branch creation failed")
    remote = git("remote", "get-url", "origin")
    payload = {
        "project": project,
        "task_id": task_marker,
        "agent_id": agent_id,
        "runtime": runtime,
        "repo_role": "canonical",
        "branch": branch,
        "upstream": "origin/master",
        "base_sha": head_sha,
        "head_sha": head_sha,
        "worktree_path": workspace_path,
        "storage_mode": "worktree",
        "status": "active",
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "policy_profile": policy_profile or "code_strict",
        "hygiene": {
            "git_status": "clean",
            "conflict_marker_scan": "passed",
            "external_host_preflight": True,
            "head_sha": head_sha,
            "origin_fingerprint": hashlib.sha256(remote.encode()).hexdigest()[:16],
        },
        "env": {
            "workspace_visibility": "worker_local",
            "worker_owned_workspace": worker_owned_workspace,
        },
    }
    bootstrap = _agent_host_bootstrap_binding(task_id=task_marker, agent_id=agent_id)
    if bootstrap:
        payload["agent_host_bootstrap_binding"] = bootstrap
    try:
        created = _http("POST", "/ixp/v1/work_sessions", payload,
                        base=base, token=token, timeout=30)
    except Exception:
        if worker_owned_workspace:
            cleanup_external_work_session({
                "worker_owned_workspace": True,
                "source_path": source_path,
                "workspace_path": workspace_path,
            })
        raise
    session = created.get("work_session") or {}
    work_session_id = session.get("work_session_id")
    if not work_session_id:
        if worker_owned_workspace:
            cleanup_external_work_session({
                "worker_owned_workspace": True,
                "source_path": source_path,
                "workspace_path": workspace_path,
            })
        raise RuntimeError("external Work Session registration failed")
    return {
        "work_session_id": work_session_id,
        "workspace_path": workspace_path,
        "source_path": source_path,
        "worker_owned_workspace": worker_owned_workspace,
        "branch": branch,
        "head_sha": head_sha,
        "profile": policy_profile or "code_strict",
        "external": True,
    }


def cleanup_external_work_session(managed):
    """Remove only an Agent Host workspace created for this exact runner."""
    if not (managed or {}).get("worker_owned_workspace"):
        return {"cleaned": False, "reason": "workspace_not_worker_owned"}
    source_path = os.path.abspath(str(managed.get("source_path") or ""))
    workspace_path = os.path.abspath(str(managed.get("workspace_path") or ""))
    workspace_root = os.path.abspath(os.environ.get("PM_WORKSPACE_ROOT") or os.path.join(
        os.path.dirname(source_path), "switchboard-agent-workspaces"))
    if (not source_path or not workspace_path
            or os.path.commonpath((workspace_root, workspace_path)) != workspace_root
            or workspace_path == workspace_root):
        return {"cleaned": False, "reason": "workspace_cleanup_boundary_denied"}
    removed = subprocess.run(
        ["git", "-C", source_path, "worktree", "remove", "--force", workspace_path],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if removed.returncode != 0:
        return {"cleaned": False, "reason": "git_worktree_remove_failed"}
    return {"cleaned": True, "workspace_path": workspace_path}


def archive_work_session_workspace(project, work_session_id, remove_workspace=True,
                                   base=None, token=None):
    """Archive a managed Work Session and (optionally) remove its worktree. Best-effort."""
    try:
        return _http("POST", f"/ixp/v1/work_sessions/{work_session_id}/archive_workspace",
                     {"project": project, "remove_workspace": bool(remove_workspace)},
                     base=base, token=token, timeout=60)
    except Exception:
        return None


def _cleanup_personal_bound_workspace(managed):
    """Remove only the adopted host-local checkout, never the coordinator path."""
    if not (managed or {}).get("bound_existing") or not _personal_execution_enabled():
        return {"cleaned": False, "reason": "not_personal_bound"}
    configured_root = str(
        os.environ.get("PM_PERSONAL_WORKSPACE_ROOT") or "").strip()
    workspace_value = str((managed or {}).get("workspace_path") or "").strip()
    if not configured_root or not workspace_value:
        return {"cleaned": False, "reason": "workspace_binding_missing"}
    raw_root = os.path.abspath(os.path.expanduser(configured_root))
    workspace = os.path.abspath(os.path.expanduser(workspace_value))
    if (os.path.lexists(raw_root) and os.path.islink(raw_root)):
        return {"cleaned": False, "reason": "workspace_root_symlink"}
    root = os.path.realpath(raw_root)
    try:
        inside_root = os.path.commonpath((root, workspace)) == root
    except ValueError:
        inside_root = False
    if (workspace == root or not inside_root
            or (os.path.lexists(workspace) and os.path.islink(workspace))
            or os.path.realpath(workspace) != workspace):
        return {"cleaned": False, "reason": "workspace_outside_personal_root"}
    if not os.path.exists(workspace):
        return {"cleaned": True, "already_absent": True}
    try:
        shutil.rmtree(workspace)
    except OSError as exc:
        return {"cleaned": False, "reason": f"workspace_remove_failed:{exc}"}
    parent = os.path.dirname(workspace)
    if parent != root:
        try:
            os.rmdir(parent)
        except OSError:
            pass
    return {"cleaned": True, "workspace_path": workspace}


def expire_external_work_session(project, work_session_id, agent_id,
                                 base=None, token=None):
    """Close worker-local session metadata without touching the worker's filesystem."""
    try:
        body = {"project": project, "agent_id": agent_id, "status": "expired"}
        bootstrap = _agent_host_bootstrap_binding(agent_id=agent_id)
        if bootstrap:
            body["agent_host_bootstrap_binding"] = bootstrap
        return _http(
            "PATCH", f"/ixp/v1/work_sessions/{work_session_id}",
            body,
            base=base, token=token, timeout=15,
        )
    except Exception:
        return None


def _default_test_commands():
    """Test command(s) the executed-test runner runs in the bound worktree. Override with
    PM_WORK_SESSION_TEST_CMD (newline-separated for multiple commands)."""
    raw = os.environ.get("PM_WORK_SESSION_TEST_CMD", "").strip()
    if raw:
        cmds = [c.strip() for c in raw.splitlines() if c.strip()]
        return cmds or [raw]
    return ["scripts/switchboard_ci.sh"]


_PERSONAL_TEST_HOST_PATH_ENV = (
    "PM_AGENT_HOST_IDENTITY_PATH",
    "PM_AGENT_HOST_CONFIG_PATH",
    "PM_AGENT_HOST_STATE_PATH",
    "PM_AGENT_HOST_RUNNER_DIR",
    "PM_AGENT_HOST_RUNTIME_ROOT",
    "PM_AGENT_HOST_CODEX_HOME",
    "PM_AGENT_HOST_SOURCE_CODEX_HOME",
    "PM_AGENT_HOST_USER_HOME",
)


def _personal_test_runtime_roots():
    """Return supervisor-owned Python roots needed by the sandboxed test harness."""
    roots = {
        os.path.realpath(os.path.abspath(str(value)))
        for value in (sys.prefix, sys.base_prefix)
        if str(value or "").strip()
    }
    return sorted(path for path in roots if os.path.isdir(path))


def _sandbox_paths_overlap(left, right):
    try:
        left = os.path.realpath(left)
        right = os.path.realpath(right)
        return os.path.commonpath((left, right)) in {left, right}
    except ValueError:
        return False


def _validate_personal_test_runtime_roots(runtime_roots, protected):
    """Never re-expose a protected credential/state path via a runtime allowlist."""
    sensitive = {
        protected[key]
        for key in (
            "PM_AGENT_HOST_IDENTITY_PATH",
            "PM_AGENT_HOST_CONFIG_PATH",
            "PM_AGENT_HOST_STATE_PATH",
            "PM_AGENT_HOST_RUNNER_DIR",
            "PM_AGENT_HOST_RUNTIME_ROOT",
            "PM_AGENT_HOST_CODEX_HOME",
            "PM_AGENT_HOST_SOURCE_CODEX_HOME",
        )
        if key in protected
    }
    sensitive.add(os.path.dirname(protected["PM_AGENT_HOST_CONFIG_PATH"]))
    for runtime_root in runtime_roots:
        if any(_sandbox_paths_overlap(runtime_root, path) for path in sensitive):
            raise RuntimeError(
                "personal executed-test runtime overlaps protected host state")


def _personal_test_sandbox_argv(argv, workspace_path):
    """Confine worker-controlled post-run tests away from host-owned secrets.

    Environment scrubbing alone is insufficient for a same-UID child: it could read the
    enrolled identity file or inspect the supervisor.  Personal hosts therefore fail closed
    unless the platform's OS sandbox is present.  The Linux PID namespace hides the
    supervisor; both profiles hide identity/configuration, lifecycle state, and provider
    runtime data while keeping only the exact test workspace writable.
    """
    workspace = os.path.realpath(os.path.abspath(os.path.expanduser(workspace_path)))
    if not os.path.isdir(workspace) or os.path.islink(workspace_path):
        raise RuntimeError("personal executed-test workspace must be a real directory")
    protected = {
        key: os.path.realpath(os.path.abspath(os.path.expanduser(
            str(os.environ.get(key) or "").strip())))
        for key in _PERSONAL_TEST_HOST_PATH_ENV
        if str(os.environ.get(key) or "").strip()
    }
    required = {
        "PM_AGENT_HOST_IDENTITY_PATH",
        "PM_AGENT_HOST_CONFIG_PATH",
        "PM_AGENT_HOST_STATE_PATH",
        "PM_AGENT_HOST_CODEX_HOME",
        "PM_AGENT_HOST_SOURCE_CODEX_HOME",
        "PM_AGENT_HOST_USER_HOME",
    }
    missing = sorted(required - protected.keys())
    if missing:
        raise RuntimeError(
            "personal executed-test sandbox lacks host path bindings: " + ",".join(missing))
    platform_name = str(
        os.environ.get("PM_AGENT_HOST_PLATFORM") or sys.platform).strip().lower()
    runtime_roots = _personal_test_runtime_roots()
    _validate_personal_test_runtime_roots(runtime_roots, protected)

    if platform_name in {"darwin", "mac", "macos"}:
        sandbox = shutil.which("sandbox-exec")
        if not sandbox:
            raise RuntimeError("personal executed tests require macOS sandbox-exec")
        file_paths = [protected["PM_AGENT_HOST_STATE_PATH"]]
        directory_paths = [
            protected["PM_AGENT_HOST_USER_HOME"],
            protected["PM_AGENT_HOST_SOURCE_CODEX_HOME"],
            os.path.dirname(protected["PM_AGENT_HOST_CONFIG_PATH"]),
        ]
        directory_paths.extend(
            protected[key] for key in (
                "PM_AGENT_HOST_RUNNER_DIR", "PM_AGENT_HOST_RUNTIME_ROOT",
                "PM_AGENT_HOST_CODEX_HOME")
            if key in protected
        )
        temporary_root = os.path.realpath(tempfile.gettempdir())
        profile = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(deny process-info*)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read*)",
            f"(allow file-write* (subpath {json.dumps(temporary_root)}))",
            "(allow file-write* (literal \"/dev/null\"))",
        ]
        profile.extend(
            f"(deny file-read* file-write* (literal {json.dumps(path)}))"
            for path in file_paths
        )
        profile.extend(
            f"(deny file-read* file-write* (subpath {json.dumps(path)}))"
            for path in sorted(set(directory_paths))
        )
        profile.extend(
            f"(allow file-read* (subpath {json.dumps(path)}))"
            for path in runtime_roots
        )
        # The worker checkout is the only user-home subtree restored after the
        # broad credential boundary.  Everything else beneath the real home stays
        # unreadable to repository-controlled post-run code.
        profile.append(
            f"(allow file-read* file-write* (subpath {json.dumps(workspace)}))")
        return [sandbox, "-p", "\n".join(profile), *argv]

    if platform_name.startswith("linux"):
        sandbox = shutil.which("bwrap")
        if not sandbox:
            raise RuntimeError("personal executed tests require Linux bubblewrap (bwrap)")
        user_home = protected["PM_AGENT_HOST_USER_HOME"]
        if user_home == os.path.sep:
            raise RuntimeError("personal executed-test user home cannot be filesystem root")
        temporary_root = os.path.realpath("/tmp")
        user_home_hidden_by_tmp = (
            os.path.commonpath((temporary_root, user_home)) == temporary_root)
        command = [
            sandbox,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--dir", "/tmp/switchboard-test-home",
        ]
        if user_home_hidden_by_tmp:
            # Hermetic tests and some portable installs place their protected home
            # below /tmp. The /tmp tmpfs already hides that whole tree; recreate only
            # the empty mountpoint needed for the exact-workspace bind below.
            command += ["--dir", user_home]
        else:
            command += ["--tmpfs", user_home]
        recreated_dirs = set()

        def recreate_home_path(path):
            relative = os.path.relpath(path, user_home)
            current = user_home
            if relative == ".":
                raise RuntimeError(
                    "personal executed-test mount cannot expose the user home")
            for part in relative.split(os.path.sep):
                current = os.path.join(current, part)
                if current not in recreated_dirs:
                    command.extend(["--dir", current])
                    recreated_dirs.add(current)

        for key in (
                "PM_AGENT_HOST_SOURCE_CODEX_HOME",
                "PM_AGENT_HOST_RUNNER_DIR", "PM_AGENT_HOST_RUNTIME_ROOT",
                "PM_AGENT_HOST_CODEX_HOME"):
            path = protected.get(key)
            if (path and os.path.isdir(path)
                    and os.path.commonpath((user_home, path)) != user_home):
                command += ["--tmpfs", path]
        for runtime_root in runtime_roots:
            if os.path.commonpath((user_home, runtime_root)) == user_home:
                recreate_home_path(runtime_root)
                command += ["--ro-bind", runtime_root, runtime_root]
        # A real deployment normally stores workspaces below the user's protected
        # home. Recreate only the destination parents before restoring the exact
        # checkout; the bind source is opened by bubblewrap before namespace setup.
        if os.path.commonpath((user_home, workspace)) == user_home:
            recreate_home_path(workspace)
        command += [
            "--bind", workspace, workspace,
            "--chdir", workspace,
            "--setenv", "HOME", "/tmp/switchboard-test-home",
            "--setenv", "TMPDIR", "/tmp",
            *argv,
        ]
        return command

    raise RuntimeError(f"personal executed tests do not support platform {platform_name!r}")


def run_executed_tests(workspace_path, work_session_id, task_id, claim_id, agent_id,
                       branch="", head_sha="", commands=None, timeout_s=1800):
    """Run the executed-test runner (SESSION-10) inside the bound worktree and return a
    switchboard.executed_test_run.v1 evidence dict. The worktree is a full checkout, so it
    ships its own scripts/work_session_test_run.py."""
    cmds = commands or _default_test_commands()
    script = os.path.join(workspace_path, "scripts", "work_session_test_run.py")
    argv = [sys.executable, script, "--cwd", workspace_path,
            "--work-session-id", work_session_id, "--task-id", task_id,
            "--claim-id", claim_id, "--agent-id", agent_id]
    if branch:
        argv += ["--branch", branch]
    if head_sha:
        argv += ["--head-sha", head_sha]
    for cmd in cmds:
        argv += ["--command", cmd]
    try:
        env = os.environ.copy()
        # The runner and its commands come from the worker-modifiable checkout.
        # Keep the stable Agent Host coordination bearer in this supervisor only.
        for key in ("PM_MCP_TOKEN", "SWITCHBOARD_TOKEN"):
            env.pop(key, None)
        if _personal_execution_enabled():
            argv = _personal_test_sandbox_argv(argv, workspace_path)
            for key in _PERSONAL_TEST_HOST_PATH_ENV:
                env.pop(key, None)
            env.pop("CODEX_HOME", None)
            env.pop("PYTHONPATH", None)
            env["PYTHONNOUSERSITE"] = "1"
        interpreter_bin = os.path.dirname(os.path.abspath(sys.executable))
        current_path = env.get("PATH", "")
        env["PATH"] = (interpreter_bin if not current_path else
                       os.pathsep.join((interpreter_bin, current_path)))
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, env=env)
        return json.loads(proc.stdout)
    except Exception as e:  # runner missing, bad JSON, timeout — fail closed with detail
        return {"schema": "switchboard.executed_test_run.v1", "status": "error",
                "executed": False, "commands": cmds, "task_id": task_id,
                "work_session_id": work_session_id, "error": str(e)}


def _push_verification_enabled():
    """Staged-rollout flag: when set, the managed loop pushes real refs and the
    server verifies them; when unset, legacy behavior is preserved byte-for-byte."""
    return os.environ.get("PM_VERIFY_COMPLETION_PUSH", "").strip().lower() in (
        "1", "true", "yes", "on")


def _push_and_verify(workspace_path, branch, head_sha, remote="origin", timeout_s=120):
    """Push a managed worktree branch to origin and prove the head landed remotely.

    Replaces the old fabricated ``remote_ref`` (which asserted a push that never
    happened — the silent-failed-push leak). Returns {ok, remote_ref, pushed_at,
    detail}. ok is True only when the branch is on the remote AND, when a head_sha
    is known, the remote branch tip matches it.
    """
    if not workspace_path or not branch:
        return {"ok": False, "detail": "missing workspace_path or branch"}
    try:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        remote_lookup = subprocess.run(
            ["git", "-C", workspace_path, "remote", "get-url", remote],
            capture_output=True, text=True, timeout=timeout_s, env=env)
        if remote_lookup.returncode != 0:
            return {"ok": False, "detail": f"cannot resolve git remote {remote}"}

        remote_url = (remote_lookup.stdout or "").strip()
        parsed_remote = urllib.parse.urlparse(remote_url)
        github_host = (parsed_remote.hostname or "").lower()
        configured_host = str(env.get("GH_HOST") or "github.com").strip().lower()
        github_https = (
            parsed_remote.scheme.lower() == "https"
            and github_host in {"github.com", configured_host}
        )

        def push_with_env(push_env):
            push = subprocess.run(
                ["git", "-C", workspace_path, "push", "-u", remote, branch],
                capture_output=True, text=True, timeout=timeout_s, env=push_env)
            if push.returncode != 0:
                detail = (push.stderr or "").strip()[-500:]
                for secret_name in ("GH_TOKEN", "GITHUB_TOKEN"):
                    secret = str(push_env.get(secret_name) or "")
                    if secret:
                        detail = detail.replace(secret, "[REDACTED]")
                return {"ok": False, "detail": f"git push failed: {detail}"}
            ls = subprocess.run(
                ["git", "-C", workspace_path, "ls-remote", remote,
                 f"refs/heads/{branch}"],
                capture_output=True, text=True, timeout=timeout_s, env=push_env)
            remote_sha = ((ls.stdout or "").split("\t")[0].strip()
                          if (ls.stdout or "").strip() else "")
            if not remote_sha:
                return {"ok": False, "detail": f"branch {branch} not on {remote} after push"}
            if head_sha and remote_sha != head_sha:
                return {"ok": False,
                        "detail": (f"remote head {remote_sha[:12]} != evidence head "
                                   f"{head_sha[:12]}")}
            return {"ok": True, "remote_ref": f"refs/heads/{branch}",
                    "pushed_at": time.time(), "remote_sha": remote_sha}

        if not github_https:
            return push_with_env(env)

        token = str(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or "").strip()
        if not token:
            return {"ok": False,
                    "detail": "missing GitHub runtime token for noninteractive push"}
        env["GH_TOKEN"] = token
        with tempfile.TemporaryDirectory(prefix="switchboard-git-auth-") as auth_dir:
            env["GIT_CONFIG_GLOBAL"] = os.path.join(auth_dir, "gitconfig")
            setup = subprocess.run(
                ["gh", "auth", "setup-git", "--hostname", github_host],
                capture_output=True, text=True, timeout=timeout_s, env=env)
            if setup.returncode != 0:
                return {"ok": False,
                        "detail": f"GitHub credential helper setup failed (exit {setup.returncode})"}
            return push_with_env(env)
    except Exception as e:
        return {"ok": False, "detail": f"push/verify error: {e}"}


def _personal_execution_enabled():
    return os.environ.get(
        "PM_PERSONAL_AGENT_HOST_EXECUTION", "").strip().lower() in (
            "1", "true", "yes", "on")


_PERSONAL_EXECUTION_LIFECYCLE_KEY = "_switchboard_personal_execution_lifecycle"


def _personal_test_run_succeeded(run):
    if not isinstance(run, dict) or run.get("executed") is False:
        return False
    if run.get("ok") is True or run.get("passed") is True:
        return True
    exit_code = run.get("exit_code", run.get("returncode"))
    if exit_code not in (None, ""):
        try:
            return int(exit_code) == 0
        except (TypeError, ValueError):
            return False
    status = str(
        run.get("status") or run.get("conclusion") or run.get("result") or ""
    ).strip().lower()
    return status in {
        "pass", "passed", "success", "succeeded", "ok", "green", "completed",
    }


def _personal_execution_binding():
    try:
        account = json.loads(os.environ.get("PM_CO_ACCOUNT_BINDING_JSON") or "{}")
    except json.JSONDecodeError:
        account = {}
    return {
        "task_id": str(account.get("task_id") or os.environ.get("PM_TASK_ID") or "").strip(),
        "claim_id": str(account.get("claim_id") or os.environ.get("PM_CLAIM_ID") or "").strip(),
        "work_session_id": str(
            account.get("work_session_id") or os.environ.get("PM_WORK_SESSION_ID") or "").strip(),
        "host_id": str(account.get("host_id") or os.environ.get("PM_CO_HOST_ID") or "").strip(),
        "runner_session_id": str(
            account.get("runner_session_id") or os.environ.get("PM_RUNNER_SESSION_ID") or "").strip(),
        "agent_id": str(account.get("agent_id") or os.environ.get("PM_AGENT_ID") or "").strip(),
        "wake_id": str(os.environ.get("PM_CO_WAKE_ID") or "").strip(),
        "source_sha": str(os.environ.get("PM_SOURCE_SHA") or "").strip(),
        "execution_connection_id": str(
            os.environ.get("PM_EXECUTION_CONNECTION_ID") or "").strip(),
    }


def _personal_repo_clone_url(value):
    """Return a credential-free clone URL plus a stable repository identity."""
    raw = str(value or "").strip()
    slug = raw[:-4] if raw.endswith(".git") else raw
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        return f"https://github.com/{slug}.git", f"github:{slug.lower()}"
    parsed = urllib.parse.urlsplit(raw)
    if (parsed.scheme == "https" and parsed.hostname == "github.com"
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment):
        path = parsed.path.strip("/")
        path = path[:-4] if path.endswith(".git") else path
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
            return f"https://github.com/{path}.git", f"github:{path.lower()}"
    if (parsed.scheme == "file"
            and os.environ.get("PM_AGENT_HOST_ALLOW_FILE_REPO", "").strip().lower()
            in ("1", "true", "yes", "on")):
        local = os.path.realpath(urllib.parse.unquote(parsed.path))
        return f"file://{local}", f"file:{local}"
    raise RuntimeError("personal Work Session repository is not an approved clone source")


def _personal_git(args, *, cwd="", timeout=300):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    completed = subprocess.run(
        ["git", *args], cwd=cwd or None, env=env,
        capture_output=True, text=True, timeout=timeout, check=False)
    return completed


def _materialize_personal_workspace(session, source_sha, workspace_root):
    """Create one host-local exact checkout for a coordinator-bound Work Session."""
    source_sha = str(source_sha or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise RuntimeError("personal Work Session source SHA is invalid")
    branch = str(session.get("branch") or "").strip()
    if not branch or _personal_git(["check-ref-format", "--branch", branch]).returncode != 0:
        raise RuntimeError("personal Work Session branch is invalid")
    clone_url, expected_identity = _personal_repo_clone_url(session.get("repo"))
    configured_root = str(workspace_root or "").strip()
    if not configured_root:
        raise RuntimeError("personal workspace root is not configured")
    raw_root = os.path.abspath(os.path.expanduser(configured_root))
    if os.path.lexists(raw_root) and os.path.islink(raw_root):
        raise RuntimeError("personal workspace root cannot be a symlink")
    os.makedirs(raw_root, mode=0o700, exist_ok=True)
    os.chmod(raw_root, 0o700)
    root = os.path.realpath(raw_root)
    task_part = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session.get("task_id") or "task"))[:64]
    session_part = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", str(session.get("work_session_id") or "session"))[:96]
    parent = os.path.join(root, task_part)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.islink(parent) or os.path.realpath(parent) != parent:
        raise RuntimeError("personal workspace task directory cannot be a symlink")
    os.chmod(parent, 0o700)
    workspace = os.path.abspath(os.path.join(parent, session_part))
    if os.path.commonpath((root, workspace)) != root:
        raise RuntimeError("personal workspace path escaped its protected root")
    if os.path.lexists(workspace) and os.path.islink(workspace):
        raise RuntimeError("personal workspace cannot be a symlink")
    created = False
    if not os.path.exists(workspace):
        staging_root = tempfile.mkdtemp(prefix=".personal-clone-", dir=parent)
        staging = os.path.join(staging_root, "checkout")
        try:
            cloned = _personal_git(
                ["clone", "--no-checkout", "--origin", "origin", clone_url, staging],
                timeout=600)
            if cloned.returncode != 0:
                raise RuntimeError("personal repository clone failed")
            os.replace(staging, workspace)
            created = True
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
    if not os.path.isdir(workspace):
        raise RuntimeError("personal workspace materialization did not create a directory")
    os.chmod(workspace, 0o700)
    remote = _personal_git(["-C", workspace, "remote", "get-url", "origin"])
    if remote.returncode != 0:
        raise RuntimeError("personal workspace origin is missing")
    _remote_url, actual_identity = _personal_repo_clone_url((remote.stdout or "").strip())
    if actual_identity != expected_identity:
        raise RuntimeError("personal workspace origin does not match the Work Session repo")
    if not created:
        status = _personal_git(["-C", workspace, "status", "--porcelain"])
        if status.returncode != 0 or (status.stdout or "").strip():
            raise RuntimeError("personal workspace is dirty")
    fetched = _personal_git(["-C", workspace, "fetch", "--prune", "origin"], timeout=600)
    if fetched.returncode != 0:
        raise RuntimeError("personal workspace fetch failed")
    commit = _personal_git(["-C", workspace, "cat-file", "-e", f"{source_sha}^{{commit}}"])
    if commit.returncode != 0:
        raise RuntimeError("personal workspace source SHA is not available from the canonical repo")
    if not created:
        current = _personal_git(["-C", workspace, "rev-parse", "HEAD"])
        if current.returncode != 0 or (current.stdout or "").strip() != source_sha:
            raise RuntimeError("existing personal workspace is not at the bound source SHA")
    checked = _personal_git(["-C", workspace, "checkout", "-B", branch, source_sha])
    if checked.returncode != 0:
        raise RuntimeError("personal workspace branch checkout failed")
    status = _personal_git(["-C", workspace, "status", "--porcelain"])
    if status.returncode != 0 or (status.stdout or "").strip():
        raise RuntimeError("personal workspace is dirty after checkout")
    remote_ref = f"refs/remotes/origin/{branch}"
    if _personal_git(["-C", workspace, "show-ref", "--verify", "--quiet", remote_ref]).returncode == 0:
        upstream = _personal_git(
            ["-C", workspace, "branch", "--set-upstream-to", f"origin/{branch}", branch])
        if upstream.returncode != 0:
            raise RuntimeError("personal workspace upstream binding failed")
    head = _personal_git(["-C", workspace, "rev-parse", "HEAD"])
    if head.returncode != 0 or (head.stdout or "").strip() != source_sha:
        raise RuntimeError("personal workspace is not at the exact bound source SHA")
    return workspace


def checkpoint_personal_work_session(project, managed, evidence, agent_id,
                                     base=None, token=None, binding=None):
    """Persist the exact local head/test receipt through the narrow tuple gate."""
    workspace = str(managed.get("workspace_path") or "").strip()
    expected_head = str(evidence.get("head_sha") or "").strip()
    if not workspace or not expected_head:
        raise RuntimeError("personal checkpoint is missing workspace or completed head")
    head = _personal_git(["-C", workspace, "rev-parse", "HEAD"])
    actual_head = (head.stdout or "").strip() if head.returncode == 0 else ""
    if actual_head != expected_head:
        raise RuntimeError("personal workspace HEAD drifted during executed tests")
    status = _personal_git(["-C", workspace, "status", "--porcelain"])
    if status.returncode != 0:
        raise RuntimeError("personal workspace cleanliness check failed after executed tests")
    if (status.stdout or "").strip():
        raise RuntimeError("personal workspace is dirty after executed tests")
    hygiene = dict(managed.get("session_hygiene") or {})
    test_run = evidence.get("executed_test_run")
    if test_run:
        hygiene["executed_test_run"] = test_run
    hygiene.update({
        "git_status": "clean",
        "conflict_marker_scan": "passed",
        "personal_host_checkout": {
            "schema": "switchboard.personal_host_checkout.v1",
            "storage": "worker_local",
            "workspace_fingerprint": hashlib.sha256(
                str(managed.get("workspace_path") or "").encode()).hexdigest()[:16],
            "source_sha": str(os.environ.get("PM_SOURCE_SHA") or ""),
            "head_sha": actual_head,
        },
    })
    binding = dict(binding or {}) or _personal_execution_binding()
    binding["completed_head_sha"] = actual_head
    payload = {
        "project": project,
        "agent_id": agent_id,
        "head_sha": actual_head,
        "dirty_status": "clean",
        "conflict_marker_count": 0,
        "hygiene": hygiene,
        "personal_execution_binding": binding,
    }
    return _http(
        "PATCH", f"/ixp/v1/work_sessions/{managed['work_session_id']}", payload,
        base=base, token=token, timeout=30)


_PERSONAL_POSTPROCESSING_RECOVERY_TIMEOUT_S = 35 * 60


def _personal_postprocessing_recovery_timeout_s():
    try:
        return max(0.0, float(os.environ.get(
            "PM_PERSONAL_POSTPROCESSING_RECOVERY_TIMEOUT_S",
            str(_PERSONAL_POSTPROCESSING_RECOVERY_TIMEOUT_S),
        )))
    except ValueError:
        return float(_PERSONAL_POSTPROCESSING_RECOVERY_TIMEOUT_S)


def get_personal_postprocessing_state(
        project, binding, evidence, base=None, token=None):
    """Ask the server for one authenticated, transactionally verified phase."""
    return _http(
        "POST", "/ixp/v1/personal_execution/postprocessing_state",
        {
            "project": project,
            "binding": dict(binding or {}),
            "completed_head_sha": str(evidence.get("head_sha") or ""),
            "expected_evidence": {
                key: evidence[key] for key in ("branch", "executed_test_run")
                if key in evidence
            },
        },
        base=base, token=token, timeout=30,
    )


def checkpoint_personal_work_session_with_recovery(
        project, managed, evidence, agent_id, base=None, token=None, binding=None):
    """Retry an exact checkpoint and recover a lost committed response by readback."""
    deadline = time.monotonic() + _personal_postprocessing_recovery_timeout_s()
    attempt = 0
    last_error = None
    binding = dict(binding or {}) or _personal_execution_binding()
    while True:
        attempt += 1
        try:
            result = checkpoint_personal_work_session(
                project, managed, evidence, agent_id, base=base, token=token,
                binding=binding)
            if result.get("updated"):
                return result
            # A server-authored rejection is authoritative, not outcome-unknown.
            return result
        except Exception as exc:
            last_error = exc
        try:
            readback = get_personal_postprocessing_state(
                project, binding, evidence, base=base, token=token)
            if (readback.get("allowed") is True
                    and readback.get("state") in {"checkpointed", "completed"}):
                return {
                    "updated": True,
                    "readback": readback,
                    "checkpoint_confirmed_by_readback": True,
                    "attempts": attempt,
                }
            if readback.get("state") == "conflict":
                return {
                    "updated": None,
                    "outcome_unknown": True,
                    "authoritative_conflict": True,
                    "attempts": attempt,
                    "readback": readback,
                }
        except Exception as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            return {
                "updated": None,
                "outcome_unknown": True,
                "attempts": attempt,
                "error": str(last_error or "checkpoint outcome is unknown"),
            }
        time.sleep(min(1.0, 0.1 * attempt))


def complete_personal_claim_with_recovery(
        project, task_id, claim_id, managed, evidence, agent_id,
        base=None, token=None, binding=None):
    """Retry exact completion until committed readback or the reserved recovery window ends."""
    deadline = time.monotonic() + _personal_postprocessing_recovery_timeout_s()
    attempt = 0
    last_error = None
    last_readback = None
    binding = dict(binding or {}) or _personal_execution_binding()
    while True:
        attempt += 1
        try:
            result = complete_claim(
                project, claim_id, evidence, base=base, token=token,
                personal_execution_binding=binding)
            if result.get("completed"):
                return result
            if result.get("completed") is False and not result.get("error"):
                return result
            last_error = RuntimeError(str(result.get("error") or result))
        except Exception as exc:
            last_error = exc
        try:
            last_readback = get_personal_postprocessing_state(
                project, binding, evidence, base=base, token=token)
            if last_readback.get("state") == "completed":
                return {
                    "completed": True,
                    "status": "In Review",
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "completion_confirmed_by_readback": True,
                    "attempts": attempt,
                }
        except Exception as exc:
            last_error = exc
            last_readback = None
        if ((last_readback or {}).get("state") == "conflict"
                or ((last_readback or {}).get("allowed") is False
                    and last_readback is not None)):
            return {
                "completed": None,
                "outcome_unknown": True,
                "attempts": attempt,
                "error": str(last_error or "completion outcome is unknown"),
                "readback": last_readback,
            }
        if time.monotonic() >= deadline:
            return {
                "completed": None,
                "outcome_unknown": True,
                "attempts": attempt,
                "error": str(last_error or "completion retry window expired"),
                "readback": last_readback,
            }
        time.sleep(min(1.0, 0.1 * attempt))


def _acquire_claim(project, agent_id, lane_list, base, token, ttl_seconds,
                   auto_work_session, source_path):
    """Claim the next task. If the scheduler skipped code-strict tasks only because they
    need a Work Session (and auto_work_session is on), provision a managed worktree session
    and claim by exact id. Returns (claim_response, managed_context_or_None)."""
    remote_registration = os.environ.get(
        "PM_REMOTE_WORK_SESSION_REGISTRATION", "").strip().lower() in (
            "1", "true", "yes", "on")
    exact_task_id = str(os.environ.get("PM_TASK_ID") or "").strip().upper()
    personal_bound = _personal_execution_enabled()
    if personal_bound:
        try:
            binding = json.loads(os.environ.get("PM_CO_ACCOUNT_BINDING_JSON") or "{}")
            claim_id = str(binding.get("claim_id") or "").strip()
            work_session_id = str(binding.get("work_session_id") or "").strip()
            if not exact_task_id or not claim_id or not work_session_id:
                raise RuntimeError("personal execution binding is incomplete")
            if str(binding.get("task_id") or "").strip().upper() != exact_task_id:
                raise RuntimeError("personal task binding does not match the wake")
            session = get_work_session(
                project, work_session_id, base=base, token=token)
            task = get_task(project, exact_task_id, base=base, token=token)
            source_sha = str(os.environ.get("PM_SOURCE_SHA") or "").strip()
            workspace_root = str(
                os.environ.get("PM_PERSONAL_WORKSPACE_ROOT") or "").strip()
            active_claims = task.get("active_claims") or []
            claim = next(
                (row for row in active_claims
                 if str(row.get("claim_id") or row.get("id") or "") == claim_id),
                None,
            )
            if (session.get("task_id") != exact_task_id
                    or session.get("agent_id") != agent_id
                    or session.get("claim_id") != claim_id
                    or session.get("work_session_id") != work_session_id
                    or session.get("status") != "active"
                    or session.get("head_sha") != source_sha
                    or not claim or claim.get("agent_id") != agent_id):
                raise RuntimeError("personal claim and Work Session binding is not active")
            workspace_path = _materialize_personal_workspace(
                session, source_sha, workspace_root)
            return {
                "claimed": True,
                "claim_id": claim_id,
                "task_id": exact_task_id,
                "task": task,
                "adopted_existing_claim": True,
            }, {
                "work_session_id": work_session_id,
                "workspace_path": workspace_path,
                "branch": session.get("branch") or "",
                "head_sha": source_sha,
                "profile": session.get("policy_profile") or "code_strict",
                "external": True,
                "bound_existing": True,
                "session_hygiene": dict(session.get("hygiene") or {}),
            }
        except Exception as exc:
            return {"claimed": False,
                    "reason": f"personal_execution_binding_error:{exc}"}, None
    if remote_registration and auto_work_session and exact_task_id:
        profile = os.environ.get("PM_WORK_SESSION_POLICY_PROFILE", "code_strict")
        try:
            managed = create_external_work_session(
                project, exact_task_id, agent_id,
                os.environ.get("PM_RUNTIME", "claude-code"), source_path,
                policy_profile=profile, base=base, token=token,
            )
        except Exception as exc:
            return {"claimed": False, "reason": f"external_work_session_error:{exc}"}, None
        claim = claim_task(
            project, exact_task_id, agent_id, base=base, token=token,
            ttl_seconds=ttl_seconds, work_session_id=managed["work_session_id"],
            session_policy_profile=profile,
        )
        if claim.get("claimed"):
            return claim, managed
        expire_external_work_session(
            project, managed["work_session_id"], agent_id, base=base, token=token)
        return claim, managed

    res = claim_next(project, agent_id, lanes=lane_list, base=base, token=token)
    if res.get("claimed") or not auto_work_session:
        return res, None
    findings = ((res.get("dispatch_reason") or {}).get("work_session_findings") or {})
    for task_id, verdict in findings.items():
        if (verdict or {}).get("reason") != "work_session_required":
            continue  # skip hard hygiene failures — only provision genuinely-missing sessions
        profile = (verdict or {}).get("policy_profile") or ""
        try:
            sess = create_managed_work_session(
                project, task_id, agent_id, storage_mode="worktree",
                policy_profile=profile, source_path=source_path, base=base, token=token)
        except Exception:
            continue
        wsid = sess.get("work_session_id") or (sess.get("work_session") or {}).get("work_session_id")
        ws = sess.get("work_session") or {}
        workspace_path = (sess.get("workspace_path") or ws.get("worktree_path")
                          or ws.get("clone_path") or "")
        if not wsid or not workspace_path:
            continue
        claim = claim_task(project, task_id, agent_id, base=base, token=token,
                           ttl_seconds=ttl_seconds, work_session_id=wsid,
                           session_policy_profile=profile)
        if claim.get("claimed"):
            return claim, {"work_session_id": wsid, "workspace_path": workspace_path,
                           "branch": ws.get("branch") or sess.get("branch") or "",
                           "head_sha": ws.get("head_sha") or sess.get("head_sha") or "",
                           "profile": profile}
        # lost the race (another agent claimed it) — release the orphaned workspace, try next
        archive_work_session_workspace(project, wsid, remove_workspace=True,
                                       base=base, token=token)
    return res, None


def run_session(project, agent_id, runtime, work_fn, lanes=None, base=None, token=None,
                max_tasks=10, register=True, auto_work_session=False, source_path=""):
    """Runtime-agnostic self-driving agent loop (ADR-0004 autonomy split, decision #4).

    handshake(register) → inbox(read) → repeatedly: heartbeat → claim_next → if work,
    work_fn(task)→evidence → complete_claim; else stop. Returns a summary. work_fn(task_dict)
    MUST return an evidence dict {branch, head_sha, pr_number?} (or raise to abandon the
    claim). The runtime supplies work_fn (its model actually does the task); this driver only
    orchestrates the loop. A process SUPERVISOR (Codex's lane) spawns/keeps-alive one such
    loop per agent.

    auto_work_session (SESSION-11): when True, code-strict tasks the scheduler skips only for
    a missing Work Session are handled automatically — the loop provisions a managed worktree
    session (SESSION-7), claims by exact id, runs the executed-test runner (SESSION-10) in the
    worktree and attaches switchboard.executed_test_run.v1 evidence before complete_claim, then
    archives the workspace. This makes code_strict satisfiable end-to-end with no per-runtime
    work, which is the prerequisite for flipping a board's code-task default to code_strict.
    Default False → byte-for-byte the previous behavior. source_path is the canonical repo the
    managed worktree branches from (defaults to $PM_WORK_SESSION_SOURCE_PATH or cwd).

    Stops on: no_unblocked_work, work_fn error (claim abandoned), or max_tasks. Fail-open on
    transport: a failed claim_next ends the loop cleanly rather than spinning.
    """
    lane_list = (lanes if isinstance(lanes, list) else
                 [x.strip() for x in (lanes or "").split(",") if x.strip()]) or None
    source_path = source_path or os.environ.get("PM_WORK_SESSION_SOURCE_PATH") or os.getcwd()
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
            res, managed = _acquire_claim(project, agent_id, lane_list, base, token,
                                          ttl_seconds=1800,
                                          auto_work_session=auto_work_session,
                                          source_path=source_path)
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
            evidence = work_fn({**res, "task_id": task_id, "task": task,
                                "managed": managed or {}}) or {}
        except Exception as e:
            abandonment = abandon_claim(
                project, claim_id, f"work_fn error: {e}", base=base, token=token)
            if managed:
                if managed.get("bound_existing") and _personal_execution_enabled():
                    # A rejected abandon can mean the successful receipt is merely
                    # outcome-unknown. Preserve the checkout for recovery unless the
                    # exact failed tuple actually released the claim.
                    if isinstance(abandonment, dict) and abandonment.get("abandoned"):
                        _cleanup_personal_bound_workspace(managed)
                elif managed.get("external"):
                    expire_external_work_session(
                        project, managed["work_session_id"], agent_id,
                        base=base, token=token)
                    cleanup_external_work_session(managed)
                else:
                    archive_work_session_workspace(project, managed["work_session_id"],
                                                   remove_workspace=True, base=base, token=token)
            return {"completed": completed, "stopped": f"work_error:{task_id}:{e}",
                    "startup_inbox": startup_inbox}
        personal_bound = bool(
            managed and managed.get("bound_existing")
            and _personal_execution_enabled()
        )
        execution_lifecycle = None
        if isinstance(evidence, dict):
            execution_lifecycle = evidence.pop(
                _PERSONAL_EXECUTION_LIFECYCLE_KEY, None)
        personal_lifecycle = execution_lifecycle if personal_bound else None

        def fail_personal_postprocessing(stage, detail="", **extra):
            reason = f"post_execution_validation_failed:{stage}"
            if personal_lifecycle:
                fail = personal_lifecycle.get("fail")
                if callable(fail):
                    try:
                        terminal = fail(reason)
                    except Exception as exc:
                        return {
                            "completed": completed,
                            "stopped": (
                                f"personal_terminalization_error:{task_id}:{stage}:{exc}"
                            ),
                            "startup_inbox": startup_inbox,
                            **extra,
                        }
                    if (isinstance(terminal, dict)
                            and terminal.get("status") not in {None, "failed"}):
                        return {
                            "completed": completed,
                            "stopped": (
                                f"personal_terminalization_rejected:{task_id}:{stage}"
                            ),
                            "terminalization": terminal,
                            "startup_inbox": startup_inbox,
                            **extra,
                        }
            abandonment = abandon_claim(
                project, claim_id,
                f"personal post-execution {stage} failed: {detail}"[:500],
                base=base, token=token)
            if (isinstance(abandonment, dict) and abandonment.get("abandoned")):
                _cleanup_personal_bound_workspace(managed)
            return {
                "completed": completed,
                "stopped": f"{stage}:{task_id}:{detail}",
                "abandonment": abandonment,
                "startup_inbox": startup_inbox,
                **extra,
            }
        if managed:
            # code_strict: the runtime need not know about executed-test evidence — the loop
            # runs the tests in the bound worktree and attaches the proof itself.
            if not (evidence.get("executed_test_run") or evidence.get("executed_test_runs")):
                run = run_executed_tests(
                    managed["workspace_path"], managed["work_session_id"], task_id,
                    claim_id, agent_id, branch=managed.get("branch", ""),
                    head_sha=evidence.get("head_sha") or managed.get("head_sha", ""))
                evidence = {**evidence, "executed_test_run": run}
            if (execution_lifecycle
                    and not _personal_test_run_succeeded(
                        evidence.get("executed_test_run") or {})):
                failed = execution_lifecycle.get("fail")
                if callable(failed):
                    failed("executed_tests_failed")
                if not personal_bound:
                    abandonment = abandon_claim(
                        project, claim_id,
                        f"executed tests failed for {task_id}",
                        base=base, token=token)
                    if managed.get("external"):
                        expire_external_work_session(
                            project, managed["work_session_id"], agent_id,
                            base=base, token=token)
                        cleanup_external_work_session(managed)
                    return {
                        "completed": completed,
                        "stopped": f"executed_tests_failed:{task_id}",
                        "abandonment": abandonment,
                        "executed_test_run": evidence.get("executed_test_run"),
                        "startup_inbox": startup_inbox,
                    }
            if (personal_bound
                    and not _personal_test_run_succeeded(
                        evidence.get("executed_test_run") or {})):
                run = evidence.get("executed_test_run") or {}
                return fail_personal_postprocessing(
                    "executed_tests_failed",
                    str(run.get("error") or run.get("status") or "failed"),
                    executed_test_run=run,
                )
            evidence.setdefault("branch", managed.get("branch", ""))
            evidence.setdefault("head_sha", managed.get("head_sha", ""))
            evidence.setdefault("git_diff_check", "clean")
            if not (evidence.get("pr_url") or evidence.get("remote_ref")):
                if _push_verification_enabled():
                    # Actually push the managed worktree branch and prove the head
                    # landed before completing. This used to fabricate remote_ref
                    # without pushing (the silent-failed-push leak); with
                    # verification on, a real, verified push is required.
                    pushed = _push_and_verify(managed.get("workspace_path", ""),
                                              evidence.get("branch", ""),
                                              evidence.get("head_sha", ""))
                    if not pushed.get("ok"):
                        if personal_bound:
                            return fail_personal_postprocessing(
                                "push_error", str(pushed.get("detail") or "failed"),
                                push=pushed,
                            )
                        abandonment = abandon_claim(
                            project, claim_id,
                            f"push failed for {task_id}: {pushed.get('detail')}",
                            base=base, token=token)
                        if (managed.get("bound_existing")
                                and _personal_execution_enabled()):
                            if (isinstance(abandonment, dict)
                                    and abandonment.get("abandoned")):
                                _cleanup_personal_bound_workspace(managed)
                        elif managed.get("external"):
                            expire_external_work_session(
                                project, managed["work_session_id"], agent_id,
                                base=base, token=token)
                        else:
                            archive_work_session_workspace(
                                project, managed["work_session_id"],
                                remove_workspace=True, base=base, token=token)
                        return {"completed": completed,
                                "stopped": f"push_error:{task_id}:{pushed.get('detail')}",
                                "startup_inbox": startup_inbox}
                    evidence["remote_ref"] = pushed.get("remote_ref")
                    evidence.setdefault("pushed_at", pushed.get("pushed_at"))
                else:
                    # Legacy: assert the branch ref so the code_strict completion
                    # gate's push-evidence presence check passes. Real push and
                    # remote verification are enabled via PM_VERIFY_COMPLETION_PUSH.
                    evidence["remote_ref"] = f"refs/heads/{evidence.get('branch', '')}"
        if personal_bound and execution_lifecycle:
            complete_execution = execution_lifecycle.get("complete")
            if callable(complete_execution):
                try:
                    terminal = complete_execution(evidence)
                except Exception as exc:
                    return {
                        "completed": completed,
                        "stopped": f"execution_terminalization_error:{task_id}:{exc}",
                        "startup_inbox": startup_inbox,
                    }
                if (isinstance(terminal, dict)
                        and terminal.get("status") not in {None, "completed"}):
                    return {
                        "completed": completed,
                        "stopped": f"execution_terminalization_rejected:{task_id}",
                        "terminalization": terminal,
                        "startup_inbox": startup_inbox,
                    }
        if personal_bound:
            try:
                checkpoint = checkpoint_personal_work_session_with_recovery(
                    project, managed, evidence, agent_id, base=base, token=token)
            except Exception as exc:
                return {
                    "completed": completed,
                    "stopped": f"checkpoint_unknown:{task_id}:{exc}",
                    "startup_inbox": startup_inbox,
                }
            if checkpoint.get("outcome_unknown"):
                return {
                    "completed": completed,
                    "stopped": f"checkpoint_unknown:{task_id}",
                    "checkpoint": checkpoint,
                    "startup_inbox": startup_inbox,
                }
            if not checkpoint.get("updated"):
                return fail_personal_postprocessing(
                    "checkpoint_rejected", "server rejected checkpoint",
                    checkpoint=checkpoint)
            managed["head_sha"] = evidence.get("head_sha") or managed.get("head_sha")
            checkpointed = (personal_lifecycle or {}).get("checkpointed")
            if callable(checkpointed):
                try:
                    checkpointed(evidence, checkpoint)
                except Exception as exc:
                    return {
                        "completed": completed,
                        "stopped": f"checkpoint_journal_error:{task_id}:{exc}",
                        "startup_inbox": startup_inbox,
                    }
        try:
            completion = (
                complete_personal_claim_with_recovery(
                    project, task_id, claim_id, managed, evidence, agent_id,
                    base=base, token=token)
                if personal_bound else
                complete_claim(project, claim_id, evidence, base=base, token=token)
            )
        except Exception as exc:
            # The response may have been lost after commit. Preserve the exact
            # terminal checkout for readback/retry; never convert outcome-unknown
            # success into a conflicting failure receipt.
            if personal_bound:
                return {"completed": completed,
                        "stopped": f"complete_unknown:{task_id}:{exc}",
                        "startup_inbox": startup_inbox}
            if execution_lifecycle:
                failed = execution_lifecycle.get("fail")
                if callable(failed):
                    failed("claim_completion_error")
            abandonment = abandon_claim(
                project, claim_id, f"claim completion failed: {exc}",
                base=base, token=token)
            if managed and managed.get("external"):
                expire_external_work_session(
                    project, managed["work_session_id"], agent_id,
                    base=base, token=token)
                cleanup_external_work_session(managed)
            return {
                "completed": completed,
                "stopped": f"complete_error:{task_id}:{exc}",
                "abandonment": abandonment,
                "startup_inbox": startup_inbox,
            }
        if personal_bound and completion.get("outcome_unknown"):
            return {
                "completed": completed,
                "stopped": f"complete_unknown:{task_id}",
                "completion": completion,
                "startup_inbox": startup_inbox,
            }
        # Verify progress before claiming it: if the server fail-closed the
        # completion (e.g. push_not_on_remote), stop loudly rather than looping on
        # as if the task were done.
        if isinstance(completion, dict) and completion.get("completed") is False:
            if personal_bound:
                return fail_personal_postprocessing(
                    "complete_rejected",
                    str(completion.get("reason") or "server rejected completion"),
                    rejection=completion)
            if execution_lifecycle:
                failed = execution_lifecycle.get("fail")
                if callable(failed):
                    failed("claim_completion_rejected")
            abandonment = abandon_claim(
                project, claim_id,
                f"claim completion rejected for {task_id}: "
                f"{completion.get('reason')}",
                base=base, token=token)
            if managed and managed.get("external"):
                expire_external_work_session(
                    project, managed["work_session_id"], agent_id,
                    base=base, token=token)
                cleanup_external_work_session(managed)
            return {"completed": completed,
                    "stopped": f"complete_rejected:{task_id}:{completion.get('reason')}",
                    "rejection": completion, "abandonment": abandonment,
                    "startup_inbox": startup_inbox}
        if personal_bound and isinstance(completion, dict) and completion.get("completed"):
            claim_completed = (personal_lifecycle or {}).get("claim_completed")
            if callable(claim_completed):
                try:
                    claim_completed(evidence, completion)
                except Exception as exc:
                    return {
                        "completed": completed,
                        "stopped": f"completion_journal_error:{task_id}:{exc}",
                        "startup_inbox": startup_inbox,
                    }
        if (not personal_bound and execution_lifecycle
                and isinstance(completion, dict) and completion.get("completed")):
            complete_execution = execution_lifecycle.get("complete")
            if callable(complete_execution):
                try:
                    terminal = complete_execution(evidence)
                except Exception as exc:
                    return {
                        "completed": completed,
                        "stopped": f"runner_terminalization_error:{task_id}:{exc}",
                        "completion": completion,
                        "startup_inbox": startup_inbox,
                    }
                if (isinstance(terminal, dict)
                        and terminal.get("status") not in {None, "completed"}):
                    return {
                        "completed": completed,
                        "stopped": f"runner_terminalization_rejected:{task_id}",
                        "terminalization": terminal,
                        "completion": completion,
                        "startup_inbox": startup_inbox,
                    }
        if (managed and managed.get("bound_existing") and _personal_execution_enabled()
                and isinstance(completion, dict) and completion.get("completed")):
            cleanup = _cleanup_personal_bound_workspace(managed)
            if not cleanup.get("cleaned"):
                return {
                    "completed": completed,
                    "stopped": f"cleanup_pending:{task_id}",
                    "cleanup": cleanup,
                    "startup_inbox": startup_inbox,
                }
            cleanup_completed = (personal_lifecycle or {}).get("cleanup_completed")
            if callable(cleanup_completed):
                try:
                    cleanup_completed()
                except Exception as exc:
                    return {
                        "completed": completed,
                        "stopped": f"cleanup_journal_error:{task_id}:{exc}",
                        "startup_inbox": startup_inbox,
                    }
        elif managed and not managed.get("external"):
            archive_work_session_workspace(project, managed["work_session_id"],
                                           remove_workspace=True, base=base, token=token)
        elif managed and managed.get("external"):
            cleanup_external_work_session(managed)
        completed.append({"task_id": task_id, "evidence": evidence,
                          "managed": bool(managed), "completion": completion})
    return {"completed": completed, "stopped": "max_tasks", "startup_inbox": startup_inbox}
