"""Native Codex worker for a user-enrolled, host-local ChatGPT login.

This path deliberately does not materialize a provider credential or acquire a
central credential lease. The user-owned Agent Host launches the already signed-in
native Codex CLI inside the exact managed workspace supplied by Switchboard.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_METERED_PROVIDER_ENV = {
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
}


def _git(workspace: str, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", workspace, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed")
    return (completed.stdout or "").strip()


def _prompt(task: dict[str, Any], *, source_sha: str, wake_id: str,
            execution_connection_id: str) -> str:
    task_id = str(task.get("task_id") or "")
    description = str(task.get("description") or "").strip()
    title = str(task.get("title") or "").strip()
    return (
        "You are the native Codex implementation worker for a task already claimed "
        "and bound by Switchboard. Work only in the current managed workspace. "
        "Do not claim or complete another task. Inspect the live task and working "
        "agreement through the configured taikun-plan MCP server, implement the task, "
        "run the required tests, commit the intended changes, and push the current "
        "task branch. Leave the worktree clean before exiting.\n\n"
        f"Task: {task_id} {title}\n"
        f"Exact source SHA: {source_sha}\n"
        f"Wake: {wake_id}\n"
        f"Execution connection: {execution_connection_id}\n\n"
        f"Task description:\n{description}"
    )


def run(
    task: dict[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    codex_executable: str = "",
) -> dict[str, Any]:
    """Run native Codex with local auth and return pushed exact-head evidence."""
    managed = task.get("managed") or {}
    try:
        account_binding = json.loads(
            os.environ.get("PM_CO_ACCOUNT_BINDING_JSON") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("native Codex account binding is invalid") from exc
    values = {
        "task_id": str(task.get("task_id") or "").strip(),
        "claim_id": str(task.get("claim_id") or task.get("id") or "").strip(),
        "work_session_id": str(managed.get("work_session_id") or "").strip(),
        "workspace": str(managed.get("workspace_path") or "").strip(),
        "host_id": str(os.environ.get("PM_CO_HOST_ID")
                       or os.environ.get("PM_HOST_ID") or "").strip(),
        "runner_session_id": str(os.environ.get("PM_RUNNER_SESSION_ID") or "").strip(),
        "wake_id": str(os.environ.get("PM_CO_WAKE_ID") or "").strip(),
        "source_sha": str(os.environ.get("PM_SOURCE_SHA") or "").strip(),
        "execution_connection_id": str(
            os.environ.get("PM_EXECUTION_CONNECTION_ID") or "").strip(),
        "agent_id": str(os.environ.get("PM_AGENT_ID") or "").strip(),
    }
    missing = sorted(key for key, value in values.items() if not value)
    if missing:
        raise RuntimeError("native Codex execution binding is incomplete: " + ",".join(missing))
    if not _SHA_RE.fullmatch(values["source_sha"]):
        raise RuntimeError("native Codex source SHA is invalid")
    relational = {
        "task_id": account_binding.get("task_id"),
        "claim_id": account_binding.get("claim_id"),
        "work_session_id": account_binding.get("work_session_id"),
        "host_id": account_binding.get("host_id"),
        "runner_session_id": account_binding.get("runner_session_id"),
        "agent_id": account_binding.get("agent_id"),
        "claim_id.environment": os.environ.get("PM_CLAIM_ID"),
        "work_session_id.environment": os.environ.get("PM_WORK_SESSION_ID"),
    }
    relational_expected = {
        "task_id": values["task_id"],
        "claim_id": values["claim_id"],
        "work_session_id": values["work_session_id"],
        "host_id": values["host_id"],
        "runner_session_id": values["runner_session_id"],
        "agent_id": values["agent_id"],
        "claim_id.environment": values["claim_id"],
        "work_session_id.environment": values["work_session_id"],
    }
    mismatches = sorted(
        key for key, value in relational.items()
        if str(value or "").strip() != relational_expected[key]
    )
    if mismatches:
        raise RuntimeError(
            "native Codex relational binding mismatch: " + ",".join(mismatches))
    workspace = values["workspace"]
    if not Path(workspace).is_dir():
        raise RuntimeError("native Codex managed workspace does not exist")
    starting_head = _git(workspace, "rev-parse", "HEAD")
    if starting_head != values["source_sha"]:
        raise RuntimeError("native Codex workspace is not at the exact bound source SHA")

    executable = str(codex_executable or shutil.which("codex") or "").strip()
    if not executable:
        raise RuntimeError("native Codex CLI is not installed")
    environment = os.environ.copy()
    for key in _METERED_PROVIDER_ENV:
        environment.pop(key, None)
    command = [
        executable,
        "exec",
        "--ephemeral",
        "-s",
        "danger-full-access",
        "-c",
        'approval_policy="never"',
        "-C",
        workspace,
        _prompt(
            task,
            source_sha=values["source_sha"],
            wake_id=values["wake_id"],
            execution_connection_id=values["execution_connection_id"],
        ),
    ]
    completed = runner(
        command,
        cwd=workspace,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=7200,
        check=False,
    )
    output = ((completed.stdout or "") + (completed.stderr or "")).encode()
    if completed.returncode != 0:
        raise RuntimeError(
            "native Codex execution failed: "
            + (completed.stderr or completed.stdout or "no output")[-1000:])

    branch = _git(workspace, "branch", "--show-current")
    head_sha = _git(workspace, "rev-parse", "HEAD")
    dirty = _git(workspace, "status", "--porcelain")
    if dirty:
        raise RuntimeError("native Codex left the managed workspace dirty")
    upstream_head = _git(workspace, "rev-parse", "@{upstream}")
    if upstream_head != head_sha:
        raise RuntimeError("native Codex did not push the exact completed head")
    return {
        "branch": branch,
        "head_sha": head_sha,
        "git_diff_check": "clean",
        "verification": {
            "schema": "switchboard.codex_host_local_execution.v1",
            "task_id": values["task_id"],
            "claim_id": values["claim_id"],
            "work_session_id": values["work_session_id"],
            "host_id": values["host_id"],
            "runner_session_id": values["runner_session_id"],
            "wake_id": values["wake_id"],
            "execution_connection_id": values["execution_connection_id"],
            "agent_id": values["agent_id"],
            "source_sha": values["source_sha"],
            "completed_head_sha": head_sha,
            "native_cli": True,
            "auth_mode": "chatgpt_personal",
            "provider_credential_exported": False,
            "metered_api_key_paths_absent": True,
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_bytes": len(output),
            "provider_output_redacted": True,
        },
    }


__all__ = ["run"]
