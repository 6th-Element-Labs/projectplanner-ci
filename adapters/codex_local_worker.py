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
import threading
import time
from typing import Any, Callable
import urllib.parse

try:
    import switchboard_core as sb
except ModuleNotFoundError:  # package import in tests and library callers
    from adapters import switchboard_core as sb


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_METERED_PROVIDER_ENV = {
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
}
_PERSONAL_EXECUTION_LIFECYCLE_KEY = "_switchboard_personal_execution_lifecycle"
_TERMINAL_WAKE_STATUSES = {"completed", "failed", "cancelled", "expired"}
_TERMINALIZATION_READBACK_TIMEOUT_S = 35 * 60


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


def _runner_record(values: dict[str, str], *, workspace: str, status: str) -> dict[str, Any]:
    return {
        "project": os.environ.get("PM_PROJECT", "switchboard"),
        "runner_session_id": values["runner_session_id"],
        "host_id": values["host_id"],
        "agent_id": values["agent_id"],
        "runtime": "codex",
        "task_id": values["task_id"],
        "claim_id": values["claim_id"],
        "status": status,
        "cwd": workspace,
        "control": {
            "tier": "T3", "runner_kill": True, "managed_process": True,
            "runner_open": True, "runner_inject": True, "runner_logs": True,
        },
        "metadata": {
            "wake_id": values["wake_id"],
            "work_session_id": values["work_session_id"],
            "source_sha": values["source_sha"],
            "execution_connection_id": values["execution_connection_id"],
            "credential_admission_phase": "claim_bound",
            "auth_lane": "chatgpt_personal_host_local",
        },
        "heartbeat_ttl_s": 180,
    }


def _update_runner(
    http: Callable[..., dict[str, Any]], values: dict[str, str], *,
    workspace: str, status: str, heartbeat: bool = False,
) -> dict[str, Any]:
    path = ("/ixp/v1/heartbeat_runner_session" if heartbeat
            else "/ixp/v1/register_runner_session")
    result = http("POST", path, _runner_record(values, workspace=workspace, status=status))
    if not result or result.get("error") or result.get("error_code"):
        raise RuntimeError("native Codex runner registry update failed")
    return result


def _complete_wake(
    http: Callable[..., dict[str, Any]], values: dict[str, str],
    result: dict[str, Any],
) -> dict[str, Any]:
    body = {
        "project": os.environ.get("PM_PROJECT", "switchboard"),
        "wake_id": values["wake_id"],
        "runner_session_id": values["runner_session_id"],
        "agent_id": values["agent_id"],
        "result": result,
    }
    expected = "completed" if result.get("started") is True else "failed"
    try:
        timeout_s = float(os.environ.get(
            "PM_PERSONAL_TERMINALIZATION_TIMEOUT_S",
            str(_TERMINALIZATION_READBACK_TIMEOUT_S),
        ))
    except ValueError:
        timeout_s = float(_TERMINALIZATION_READBACK_TIMEOUT_S)
    deadline = time.monotonic() + max(0.0, timeout_s)
    last_error: Exception | None = None
    last_status = ""
    while True:
        for attempt in range(3):
            try:
                completed = http("POST", "/txp/v1/complete_wake", body)
                if (not completed or completed.get("error")
                        or completed.get("error_code")
                        or completed.get("status") != expected):
                    raise RuntimeError("native Codex wake completion was not exact")
                return completed
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))

        # A lost response is not a failed completion.  Read the durable wake
        # before deciding whether another identical write is needed.  This
        # keeps the worker alive through a transient outage instead of exiting
        # with a claimed wake and an otherwise successful checkout stranded.
        query = urllib.parse.urlencode({
            "project": body["project"],
            "host_id": values["host_id"],
            "runtime": "codex",
        })
        try:
            listed = http("GET", f"/txp/v1/list_wake_intents?{query}", None)
            wakes = ((listed or {}).get("wake_intents")
                     or (listed or {}).get("wakes") or [])
            wake = next(
                (item for item in wakes
                 if str(item.get("wake_id") or "") == values["wake_id"]),
                None,
            )
            if wake is None:
                raise RuntimeError("native Codex wake readback did not find exact wake")
            last_status = str(wake.get("status") or "")
            if last_status == expected:
                return {
                    **wake,
                    "status": expected,
                    "completion_confirmed_by_readback": True,
                }
        except Exception as exc:
            last_error = exc

        if last_status in _TERMINAL_WAKE_STATUSES:
            raise RuntimeError(
                f"native Codex wake terminalized as {last_status}, expected {expected}"
            ) from last_error

        if time.monotonic() >= deadline:
            detail = f"; authoritative status={last_status}" if last_status else ""
            raise RuntimeError(
                f"native Codex wake completion failed after readback{detail}"
            ) from last_error
        time.sleep(1.0)


def run(
    task: dict[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    codex_executable: str = "",
    http: Callable[..., dict[str, Any]] = sb._http,
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

    requested_executable = str(
        codex_executable or os.environ.get("PM_CODEX_EXECUTABLE") or "codex").strip()
    resolved_executable = shutil.which(requested_executable)
    if not resolved_executable:
        raise RuntimeError("native Codex CLI is not installed")
    executable = str(Path(resolved_executable).resolve())
    if not Path(executable).is_absolute():
        raise RuntimeError("native Codex CLI path is not absolute")
    git_common_dir = Path(_git(
        workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    try:
        git_common_dir.relative_to(Path(workspace).resolve())
        git_dirs: list[str] = []
    except ValueError:
        # Linked worktrees keep all mutable Git metadata beneath this one common
        # directory. Grant exactly that repository resource, never a home-level path.
        git_dirs = [str(git_common_dir)]
    environment = os.environ.copy()
    for key in _METERED_PROVIDER_ENV:
        environment.pop(key, None)
    command = [
        executable,
        "exec",
        "--ephemeral",
        "-s",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-c",
        'approval_policy="never"',
        "-C",
        workspace,
        *[value for directory in git_dirs for value in ("--add-dir", directory)],
        _prompt(
            task,
            source_sha=values["source_sha"],
            wake_id=values["wake_id"],
            execution_connection_id=values["execution_connection_id"],
        ),
    ]
    stop_heartbeat = threading.Event()
    heartbeat_errors: list[Exception] = []

    def heartbeat_loop() -> None:
        while not stop_heartbeat.wait(30):
            try:
                _update_runner(
                    http, values, workspace=workspace, status="running", heartbeat=True)
            except Exception as exc:  # surfaced by the mandatory final heartbeat
                heartbeat_errors.append(exc)

    runner_registered = False
    wake_completed = False
    heartbeat_thread: threading.Thread | None = None
    try:
        _update_runner(http, values, workspace=workspace, status="running")
        runner_registered = True
        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            name=f"switchboard-heartbeat-{values['runner_session_id']}",
            daemon=True,
        )
        heartbeat_thread.start()
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
        # A final running heartbeat is useful for claim renewal, but a transient
        # outage here must not decide the native execution outcome.
        try:
            _update_runner(
                http, values, workspace=workspace, status="running", heartbeat=True)
        except Exception as exc:
            heartbeat_errors.append(exc)

        branch = _git(workspace, "branch", "--show-current")
        head_sha = _git(workspace, "rev-parse", "HEAD")
        dirty = _git(workspace, "status", "--porcelain")
        if dirty:
            raise RuntimeError("native Codex left the managed workspace dirty")
        remote_ref = f"refs/heads/{branch}"
        remote_lines = _git(
            workspace, "ls-remote", "--exit-code", "--refs", "origin", remote_ref,
        ).splitlines()
        remote_heads = {
            line.split()[0]
            for line in remote_lines
            if len(line.split()) == 2 and line.split()[1] == remote_ref
        }
        if remote_heads != {head_sha}:
            raise RuntimeError("native Codex did not push the exact completed head")
        evidence = {
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
                "runner_heartbeat_errors_recovered": len(heartbeat_errors),
            },
        }
        lifecycle_lock = threading.Lock()
        lifecycle_state = {"terminal": ""}

        def finalize(succeeded: bool, reason: str = "") -> dict[str, Any]:
            nonlocal wake_completed
            with lifecycle_lock:
                terminal = lifecycle_state["terminal"]
                desired = "completed" if succeeded else "failed"
                if terminal == desired:
                    return {"status": desired, "idempotent": True}
                if terminal == "completing" and not succeeded:
                    raise RuntimeError(
                        "native Codex success completion is outcome-unknown")
                stop_heartbeat.set()
                if heartbeat_thread is not None:
                    heartbeat_thread.join()
                if succeeded:
                    lifecycle_state["terminal"] = "completing"
                    # Checkpoint and claim finalization require both terminal records.
                    # Publish them only after the outer executed-test gate succeeds.
                    _update_runner(
                        http, values, workspace=workspace, status="completed")
                    result = _complete_wake(http, values, {
                        "started": True,
                        "reason": "native_codex_execution_completed",
                        "task_id": values["task_id"],
                        "branch": branch,
                        "head_sha": head_sha,
                    })
                elif terminal == "completed":
                    # A later checkpoint/completion rejection uses the narrow,
                    # server-validated completed -> failed recovery edge.
                    result = _complete_wake(http, values, {
                        "started": False,
                        "reason": reason or "post_execution_validation_failed",
                        "task_id": values["task_id"],
                        "recoverable_post_execution_failure": True,
                    })
                else:
                    _update_runner(http, values, workspace=workspace, status="failed")
                    result = _complete_wake(http, values, {
                        "started": False,
                        "reason": reason or "post_execution_validation_failed",
                        "task_id": values["task_id"],
                    })
                lifecycle_state["terminal"] = desired
                wake_completed = True
                return result

        evidence[_PERSONAL_EXECUTION_LIFECYCLE_KEY] = {
            "complete": lambda: finalize(True),
            "fail": lambda reason="": finalize(False, reason),
        }
        return evidence
    except Exception:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join()
        if runner_registered and not wake_completed:
            try:
                # A failed personal wake receipt is accepted only after the exact
                # bound runner is terminal. Publish that state before completion so
                # the repository can distinguish a real launch failure from a
                # still-running worker trying to abandon its wake.
                _update_runner(
                    http, values, workspace=workspace, status="failed")
                _complete_wake(http, values, {
                    "started": False,
                    "reason": "native_codex_execution_failed",
                    "task_id": values["task_id"],
                })
                wake_completed = True
            except Exception:
                # The terminal runner tuple is durable and the identical wake receipt
                # remains safe to retry after an outcome-unknown response.
                raise
        raise


__all__ = ["run"]
