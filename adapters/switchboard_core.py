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
    if task_marker.lower() not in branch.lower():
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
        "worktree_path": source_path,
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
        "env": {"workspace_visibility": "worker_local"},
    }
    created = _http("POST", "/ixp/v1/work_sessions", payload,
                    base=base, token=token, timeout=30)
    session = created.get("work_session") or {}
    work_session_id = session.get("work_session_id")
    if not work_session_id:
        raise RuntimeError("external Work Session registration failed")
    return {
        "work_session_id": work_session_id,
        "workspace_path": source_path,
        "branch": branch,
        "head_sha": head_sha,
        "profile": policy_profile or "code_strict",
        "external": True,
    }


def archive_work_session_workspace(project, work_session_id, remove_workspace=True,
                                   base=None, token=None):
    """Archive a managed Work Session and (optionally) remove its worktree. Best-effort."""
    try:
        return _http("POST", f"/ixp/v1/work_sessions/{work_session_id}/archive_workspace",
                     {"project": project, "remove_workspace": bool(remove_workspace)},
                     base=base, token=token, timeout=60)
    except Exception:
        return None


def expire_external_work_session(project, work_session_id, agent_id,
                                 base=None, token=None):
    """Close worker-local session metadata without touching the worker's filesystem."""
    try:
        return _http(
            "PATCH", f"/ixp/v1/work_sessions/{work_session_id}",
            {"project": project, "agent_id": agent_id, "status": "expired"},
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


def _acquire_claim(project, agent_id, lane_list, base, token, ttl_seconds,
                   auto_work_session, source_path):
    """Claim the next task. If the scheduler skipped code-strict tasks only because they
    need a Work Session (and auto_work_session is on), provision a managed worktree session
    and claim by exact id. Returns (claim_response, managed_context_or_None)."""
    remote_registration = os.environ.get(
        "PM_REMOTE_WORK_SESSION_REGISTRATION", "").strip().lower() in (
            "1", "true", "yes", "on")
    exact_task_id = str(os.environ.get("PM_TASK_ID") or "").strip().upper()
    personal_bound = os.environ.get(
        "PM_PERSONAL_AGENT_HOST_EXECUTION", "").strip().lower() in (
            "1", "true", "yes", "on")
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
            workspace_path = str(
                session.get("worktree_path") or session.get("clone_path") or "").strip()
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
            if not workspace_path or not os.path.isdir(workspace_path):
                raise RuntimeError("personal bound workspace is not present on this host")
            head = subprocess.run(
                ["git", "-C", workspace_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=30, check=False)
            if head.returncode != 0 or (head.stdout or "").strip() != source_sha:
                raise RuntimeError("personal bound workspace is not at the exact source SHA")
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
            abandon_claim(project, claim_id, f"work_fn error: {e}", base=base, token=token)
            if managed:
                if managed.get("external"):
                    expire_external_work_session(
                        project, managed["work_session_id"], agent_id,
                        base=base, token=token)
                else:
                    archive_work_session_workspace(project, managed["work_session_id"],
                                                   remove_workspace=True, base=base, token=token)
            return {"completed": completed, "stopped": f"work_error:{task_id}:{e}",
                    "startup_inbox": startup_inbox}
        if managed:
            # code_strict: the runtime need not know about executed-test evidence — the loop
            # runs the tests in the bound worktree and attaches the proof itself.
            if not (evidence.get("executed_test_run") or evidence.get("executed_test_runs")):
                run = run_executed_tests(
                    managed["workspace_path"], managed["work_session_id"], task_id,
                    claim_id, agent_id, branch=managed.get("branch", ""),
                    head_sha=evidence.get("head_sha") or managed.get("head_sha", ""))
                evidence = {**evidence, "executed_test_run": run}
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
                        abandon_claim(project, claim_id,
                                      f"push failed for {task_id}: {pushed.get('detail')}",
                                      base=base, token=token)
                        if managed.get("external"):
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
        completion = complete_claim(project, claim_id, evidence, base=base, token=token)
        if managed and not managed.get("external"):
            archive_work_session_workspace(project, managed["work_session_id"],
                                           remove_workspace=True, base=base, token=token)
        # Verify progress before claiming it: if the server fail-closed the
        # completion (e.g. push_not_on_remote), stop loudly rather than looping on
        # as if the task were done.
        if isinstance(completion, dict) and completion.get("completed") is False:
            return {"completed": completed,
                    "stopped": f"complete_rejected:{task_id}:{completion.get('reason')}",
                    "rejection": completion, "startup_inbox": startup_inbox}
        completed.append({"task_id": task_id, "evidence": evidence,
                          "managed": bool(managed), "completion": completion})
    return {"completed": completed, "stopped": "max_tasks", "startup_inbox": startup_inbox}
