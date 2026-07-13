#!/usr/bin/env python3
"""Trigger-only Agent Host for Anthropic-hosted Claude Code cloud sessions.

This process consumes only wakes that explicitly require the ``vendor_cloud``
capability.  It prepares and pushes a non-default task branch, invokes the
official ``claude --cloud`` CLI bridge through :mod:`adapters.claude_cloud`, and
binds the app-visible session receipt back to the wake/runner registry/Tally.

No coding agent runs on this host.  The short-lived local clone exists only so
Claude's CLI can identify the exact pushed repository branch that its hosted VM
must clone.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters import switchboard_core as sb
from adapters.claude_cloud import (
    CANONICAL_REPO,
    TOKEN_REF,
    ClaudeCloudAdapter,
)


PROJECT = os.environ.get("PM_PROJECT", "switchboard")
RUNTIME = "claude-code"
CAPABILITY = "vendor_cloud"
P_REGISTER_HOST = "/ixp/v1/register_host"
P_HEARTBEAT_HOST = "/ixp/v1/heartbeat_host"
P_LIST_WAKES = "/txp/v1/list_wake_intents"
P_CLAIM_WAKE = "/txp/v1/claim_wake"
P_COMPLETE_WAKE = "/txp/v1/complete_wake"
P_REGISTER_RUNNER = "/ixp/v1/register_runner_session"
P_LIST_RUNNERS = "/ixp/v1/runner_sessions"
P_TALLY = "/tally/v1/spend/ingest"


def _csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").replace("\n", ",").split(",")
            if item.strip()]


def _try(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        return sb._http(method, path, body)
    except Exception as exc:
        print(f"[claude_cloud_host] {method} {path} failed: {type(exc).__name__}", flush=True)
        return None


def inventory() -> dict[str, Any]:
    lanes = _csv(os.environ.get("PM_HOST_LANES", ""))
    policy = {
        "mode": "vendor_cloud_trigger",
        "allow_message_only": False,
        "allow_work": True,
        "allow_global_claim": False,
        "allowed_lanes": lanes,
        "compute_location": "anthropic_hosted",
    }
    return {
        "project": PROJECT,
        "host_id": os.environ.get("PM_HOST_ID") or
                   f"host/{socket.gethostname().split('.')[0]}-claude-cloud",
        "hostname": socket.gethostname(),
        "agent_host_version": "0.1.0",
        "repo_root": os.environ.get("PM_REPO_ROOT") or os.getcwd(),
        "policy": policy,
        "runtimes": [{
            "runtime": RUNTIME,
            "launcher": "claude --cloud",
            "profiles": ["ixp.v1", "switchboard.cloud_execution_adapter.v1"],
            "control": {
                "mode": "provider_app",
                "runner_kill": False,
                "managed_process": False,
                "provider": "anthropic",
            },
            "policy": policy,
            "lanes": lanes,
            "capabilities": [CAPABILITY, "github", "mcp", "tests"],
        }],
        "limits": {"max_sessions": int(os.environ.get("PM_HOST_MAX_SESSIONS", "4"))},
        "heartbeat_ttl_s": 60,
    }


def eligible(wake: dict[str, Any], inv: dict[str, Any]) -> bool:
    selector = (wake or {}).get("selector") or {}
    policy = (wake or {}).get("policy") or {}
    if selector.get("runtime") != RUNTIME:
        return False
    required = set(selector.get("capabilities") or [])
    if CAPABILITY not in required or policy.get("mode") != "vendor_cloud":
        return False
    lane = selector.get("lane") or ""
    lanes = set((inv.get("runtimes") or [{}])[0].get("lanes") or [])
    return bool(lane and lane in lanes)


def task_branch(task_id: str) -> str:
    normalized = (task_id or "").strip().lower()
    if not normalized or not all(ch.isalnum() or ch == "-" for ch in normalized):
        raise ValueError("task_id is not safe for a provider branch")
    return f"claude/{normalized}-cloud"


def _run(command: list[str], cwd: str | Path, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def prepare_trigger_clone(
    repo_root: str | Path,
    task_id: str,
    wake_id: str,
    *,
    clone_root: str | Path | None = None,
    run: Callable[..., subprocess.CompletedProcess] = _run,
) -> tuple[Path, str, str]:
    """Push an exact task branch and clone it into a host-owned temporary directory."""
    root = Path(repo_root).resolve()
    branch = task_branch(task_id)
    run(["git", "fetch", "origin", "master"], cwd=root, timeout=90).check_returncode()
    base_sha = (run(["git", "rev-parse", "origin/master"], cwd=root).stdout or "").strip()
    remote = run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=root,
    )
    if remote.returncode != 0:
        run(
            ["git", "push", "origin", f"{base_sha}:refs/heads/{branch}"],
            cwd=root,
            timeout=90,
        ).check_returncode()
    remote_url = (run(["git", "remote", "get-url", "origin"], cwd=root).stdout or "").strip()
    namespace = hashlib.sha256(wake_id.encode("utf-8")).hexdigest()[:16]
    parent = Path(clone_root or os.environ.get("PM_CLAUDE_CLOUD_CLONE_ROOT") or
                  Path(tempfile.gettempdir()) / "switchboard-claude-cloud")
    parent.mkdir(parents=True, exist_ok=True)
    clone = parent / namespace
    if clone.exists():
        checked_branch = (run(["git", "branch", "--show-current"], cwd=clone).stdout or "").strip()
        dirty = (run(["git", "status", "--porcelain"], cwd=clone).stdout or "").strip()
        if checked_branch != branch or dirty:
            raise RuntimeError("existing trigger clone failed branch/hygiene validation")
    else:
        run(
            ["git", "clone", "--quiet", "--single-branch", "--branch", branch,
             remote_url, str(clone)],
            cwd=parent,
            timeout=180,
        ).check_returncode()
    run(["git", "fetch", "origin", "master"], cwd=clone, timeout=90).check_returncode()
    head_sha = (run(["git", "rev-parse", "HEAD"], cwd=clone).stdout or "").strip()
    pushed = run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=clone,
    )
    pushed_sha = ((pushed.stdout or "").split() or [""])[0]
    if not head_sha or pushed.returncode != 0 or pushed_sha != head_sha:
        raise RuntimeError("provider task branch is not pushed at the trigger SHA")
    return clone, branch, head_sha


def build_dev_brief(task_id: str, branch: str) -> str:
    """Prompt contains task coordinates and safety rules, never a bearer credential."""
    return f"""Execute Switchboard task {task_id} on project=switchboard.

Start by using the taikun-plan MCP tools to read the live task and working agreement, register
as claude/{task_id}, drain inbox, and claim exactly {task_id}. Work only in the canonical
repository {CANONICAL_REPO} on the already checked-out branch {branch}. Never switch to, push to,
or merge main/master. Implement the task completely, run the relevant tests, commit and push the
task branch, and open a pull request that names {task_id}. Report honest subscription usage to
Tally with cost_usd=0 and confidence=unknown unless a provider-measured charge is available.
Finish with complete_claim carrying branch, exact head SHA, PR URL, git diff check, and executed
test evidence. If MCP auth, GitHub access, permissions, or any required setup is missing, fail
visibly and do not substitute local/self-hosted execution or fabricate a receipt."""


def active_cloud_sessions(call: Callable[..., dict[str, Any] | None] = _try) -> int:
    query = urlencode({"project": PROJECT, "runtime": RUNTIME, "include_stale": "true"})
    listed = call("GET", f"{P_LIST_RUNNERS}?{query}") or {}
    sessions = listed.get("sessions") or []
    return sum(
        1 for session in sessions
        if not session.get("stale")
        and (session.get("metadata") or {}).get("vendor_id") == "claude-code-cloud"
        and session.get("status") in {"queued", "running", "unknown"}
    )


def process_wake(
    wake: dict[str, Any],
    inv: dict[str, Any],
    *,
    call: Callable[..., dict[str, Any] | None] = _try,
    adapter_factory: Callable[[Path], ClaudeCloudAdapter] = ClaudeCloudAdapter,
    prepare: Callable[..., tuple[Path, str, str]] = prepare_trigger_clone,
) -> dict[str, Any]:
    wake_id = wake.get("wake_id") or ""
    task_id = wake.get("task_id") or ""
    claimed = call("POST", P_CLAIM_WAKE, {
        "project": PROJECT, "host_id": inv["host_id"], "wake_id": wake_id,
    }) or {}
    if not claimed.get("claimed"):
        return {"wake_id": wake_id, "started": False,
                "reason": claimed.get("reason") or "wake_claim_failed"}

    clone: Path | None = None
    try:
        clone, branch, head_sha = prepare(inv["repo_root"], task_id, wake_id)
        dispatch = {
            "schema": "switchboard.cloud_dispatch.v1",
            "project": PROJECT,
            "task_id": task_id,
            "claim_id": "",
            "wake_id": wake_id,
            "dev_brief": build_dev_brief(task_id, branch),
            "canonical_repo": CANONICAL_REPO,
            "branch": branch,
            "head_sha": head_sha,
            "active_sessions": active_cloud_sessions(call),
            "continuity": "fresh_only",
            "mcp_access": {
                "endpoint": "https://plan.taikunai.com/mcp",
                "token_ref": TOKEN_REF,
                "scopes": ["read:task", "write:claim", "write:evidence"],
                "expires_at": time.time() + 7200,
            },
        }
        receipt = adapter_factory(clone).trigger(dispatch)
        if not receipt.get("adopted"):
            result = {
                "started": False,
                "reason": receipt.get("provider_error") or receipt.get("error") or
                          "cloud_session_not_adopted",
                "vendor_id": "claude-code-cloud",
                "branch": branch,
                "head_sha": head_sha,
            }
            call("POST", P_COMPLETE_WAKE, {
                "project": PROJECT, "wake_id": wake_id, "result": result,
            })
            return {"wake_id": wake_id, **result}

        runner_session_id = receipt["runner_session_id"]
        metadata = {
            "wake_id": wake_id,
            "vendor_id": "claude-code-cloud",
            "provider_session_id": receipt["provider_session_id"],
            "session_url": receipt["session_url"],
            "branch": branch,
            "head_sha": head_sha,
            "billing_mode": "subscription",
        }
        runner = call("POST", P_REGISTER_RUNNER, {
            "project": PROJECT,
            "runner_session_id": runner_session_id,
            "host_id": inv["host_id"],
            "agent_id": f"claude/{task_id}",
            "runtime": RUNTIME,
            "task_id": task_id,
            "status": "running",
            "cwd": str(clone),
            "control": {
                "tier": "T1",
                "managed_process": False,
                "runner_kill": False,
                "provider_app": True,
            },
            "metadata": metadata,
            "heartbeat_ttl_s": 86400,
        }) or {}
        if runner.get("error"):
            result = {"started": False, "reason": "runner_binding_failed",
                      "vendor_id": "claude-code-cloud", "session_url": receipt["session_url"]}
            call("POST", P_COMPLETE_WAKE, {"project": PROJECT, "wake_id": wake_id,
                                            "result": result})
            return {"wake_id": wake_id, **result}
        result = {
            "started": True,
            "reason": "provider_session_adopted",
            "vendor_id": "claude-code-cloud",
            "provider_session_id": receipt["provider_session_id"],
            "session_url": receipt["session_url"],
            "runner_session_id": runner_session_id,
            "task_id": task_id,
            "branch": branch,
            "head_sha": head_sha,
            "cwd": str(clone),
            "billing_mode": "subscription",
            # complete_wake upserts the runner receipt after the initial binding.
            # Carry the provider-session lifetime through that second write so the
            # live Claude session does not become stale after the store default (60s).
            "heartbeat_ttl_s": 86400,
            "control": {"provider_app": True, "runner_kill": False},
        }
        call("POST", P_COMPLETE_WAKE, {
            "project": PROJECT,
            "wake_id": wake_id,
            "runner_session_id": runner_session_id,
            "agent_id": f"claude/{task_id}",
            "result": result,
        })
        call("POST", P_TALLY, {
            "project": PROJECT,
            "source": "agent_report",
            "confidence": "unknown",
            "task_id": task_id,
            "agent_id": f"claude/{task_id}",
            "runtime": RUNTIME,
            "provider": "anthropic",
            "model": "subscription",
            "cost_usd": 0,
            "status": "running",
            "request_id": f"claude-cloud:{wake_id}",
            "metadata": {**metadata, "usage_note": "shared plan allocation; exact task cost unavailable"},
        })
        return {"wake_id": wake_id, **result}
    except Exception as exc:
        result = {"started": False, "reason": "claude_cloud_host_error",
                  "error_type": type(exc).__name__, "vendor_id": "claude-code-cloud"}
        call("POST", P_COMPLETE_WAKE, {"project": PROJECT, "wake_id": wake_id,
                                        "result": result})
        return {"wake_id": wake_id, **result}
    finally:
        if clone is not None and clone.exists():
            shutil.rmtree(clone, ignore_errors=True)


def run_once(inv: dict[str, Any] | None = None,
             call: Callable[..., dict[str, Any] | None] = _try) -> dict[str, Any]:
    inv = inv or inventory()
    active = active_cloud_sessions(call)
    call("POST", P_HEARTBEAT_HOST, {
        "project": PROJECT, "host_id": inv["host_id"], "active_sessions": active,
    })
    listed = call("GET", f"{P_LIST_WAKES}?{urlencode({'project': PROJECT, 'status': 'pending'})}") or {}
    wakes = listed.get("wake_intents") or listed.get("wakes") or []
    cap = int((inv.get("limits") or {}).get("max_sessions") or 4)
    acted: list[dict[str, Any]] = []
    for wake in wakes:
        if active + len([item for item in acted if item.get("started")]) >= cap:
            break
        if eligible(wake, inv):
            acted.append(process_wake(wake, inv, call=call))
    return {"host_id": inv["host_id"], "pending": len(wakes), "active": active,
            "acted": acted}


def run(interval: float = 10, once: bool = False) -> None:
    inv = inventory()
    last_register = 0.0
    while True:
        now = time.time()
        if now - last_register >= 30:
            _try("POST", P_REGISTER_HOST, inv)
            last_register = now
        print(run_once(inv), flush=True)
        if once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Switchboard Claude Code cloud trigger host")
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(args.interval, args.once)
