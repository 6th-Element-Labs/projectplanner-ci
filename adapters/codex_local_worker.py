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
import selectors
import shutil
import subprocess
import sys
import tempfile
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
_COORDINATION_CREDENTIAL_ENV = {
    "PM_MCP_TOKEN",
    "SWITCHBOARD_TOKEN",
}
_PERSONAL_EXECUTION_LIFECYCLE_KEY = "_switchboard_personal_execution_lifecycle"
_TERMINAL_WAKE_STATUSES = {"completed", "failed", "cancelled", "expired"}
_TERMINALIZATION_READBACK_TIMEOUT_S = 35 * 60
_RECOVERY_SCHEMA = "switchboard.personal_postprocessing_recovery.v1"


def _recovery_root() -> Path:
    configured = str(os.environ.get("PM_AGENT_HOST_RUNNER_DIR") or "").strip()
    if not configured:
        raise RuntimeError("personal post-processing recovery root is not configured")
    runner_root = Path(configured).expanduser()
    if not runner_root.is_absolute() or runner_root.is_symlink():
        raise RuntimeError("personal post-processing recovery root is unsafe")
    runner_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(runner_root, 0o700)
    root = runner_root / "postprocessing-recovery"
    if root.exists() and root.is_symlink():
        raise RuntimeError("personal post-processing recovery directory is unsafe")
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root.resolve()


def _recovery_path(values: dict[str, str]) -> Path:
    connection_id = str(values.get("execution_connection_id") or "").strip()
    if not connection_id:
        raise RuntimeError("personal recovery execution connection is missing")
    name = hashlib.sha256(connection_id.encode()).hexdigest() + ".json"
    return _recovery_root() / name


def _atomic_recovery_json(path: Path, value: dict[str, Any]) -> None:
    root = _recovery_root()
    if path.parent.resolve() != root or path.is_symlink():
        raise RuntimeError("personal recovery receipt path is unsafe")
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    fd, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=root)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _recovery_binding(values: dict[str, str]) -> dict[str, str]:
    return {
        key: values[key] for key in (
            "task_id", "claim_id", "work_session_id", "runner_session_id",
            "host_id", "agent_id", "wake_id", "source_sha",
            "execution_connection_id",
        )
    }


def _write_recovery_receipt(
        path: Path, values: dict[str, str], managed: dict[str, Any],
        evidence: dict[str, Any], stage: str) -> dict[str, Any]:
    now = time.time()
    existing: dict[str, Any] = {}
    if path.is_file() and not path.is_symlink():
        existing = json.loads(path.read_text(encoding="utf-8"))
    receipt = {
        "schema": _RECOVERY_SCHEMA,
        "project": os.environ.get("PM_PROJECT", "switchboard"),
        "task_id": values["task_id"],
        "claim_id": values["claim_id"],
        "agent_id": values["agent_id"],
        "binding": _recovery_binding(values),
        "managed": {
            "work_session_id": values["work_session_id"],
            "workspace_path": values["workspace"],
            "session_hygiene": dict(managed.get("session_hygiene") or {}),
            "bound_existing": True,
        },
        "evidence": json.loads(json.dumps(evidence, sort_keys=True)),
        "terminal_result": {
            "started": True,
            "reason": "native_codex_execution_completed",
            "task_id": values["task_id"],
            "branch": evidence.get("branch"),
            "head_sha": evidence.get("head_sha"),
        },
        "stage": stage,
        "created_at": float(existing.get("created_at") or now),
        "updated_at": now,
        "recovery_deadline": float(existing.get("recovery_deadline")
                                   or (now + _TERMINALIZATION_READBACK_TIMEOUT_S)),
    }
    _atomic_recovery_json(path, receipt)
    return receipt


def _load_recovery_receipt(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("personal recovery receipt must be a regular file")
    if path.parent.resolve() != _recovery_root():
        raise RuntimeError("personal recovery receipt escaped its root")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("schema") != _RECOVERY_SCHEMA:
        raise RuntimeError("personal recovery receipt schema is invalid")
    return receipt


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
    task_record = task.get("task") if isinstance(task.get("task"), dict) else {}
    deliverable = task.get("deliverable") or task_record.get("deliverable") or {}
    deliverable_id = str(
        task.get("deliverable_id") or task_record.get("deliverable_id")
        or (deliverable.get("id") if isinstance(deliverable, dict) else deliverable)
        or "").strip()
    project = str(os.environ.get("PM_PROJECT") or "switchboard").strip()
    # Switchboard owns the assignment and exposes it through the preloaded MCP
    # server.  Keeping the launch command this small makes the same boot contract
    # work for one task, deliverable fan-out, and concurrent cross-board runs.
    scope = f" for deliverable {deliverable_id}" if deliverable_id else ""
    return f"Do {task_id}{scope} in project {project} via Switchboard."


def _work_session_mcp_bootstrap(
        http: Callable[..., dict[str, Any]], values: dict[str, str]) -> tuple[str, list[str]]:
    """Issue the child-only bearer and return one-run Codex MCP overrides."""
    binding = _recovery_binding(values)
    result = http(
        "POST",
        f"/ixp/v1/work_sessions/{values['work_session_id']}/mcp_token",
        {"project": os.environ.get("PM_PROJECT", "switchboard"),
         "binding": binding},
    )
    token = str(result.get("token") or "").strip()
    if (result.get("issued") is not True or not token.startswith("wst-")):
        raise RuntimeError("native Codex Work Session MCP bootstrap was denied")
    base = str(os.environ.get("PM_BASE") or "https://plan.taikunai.com").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("native Codex Switchboard MCP endpoint is invalid")
    endpoint = base + "/mcp"
    overrides = [
        f'mcp_servers.taikun_plan.url={json.dumps(endpoint)}',
        'mcp_servers.taikun_plan.bearer_token_env_var="SWITCHBOARD_WORK_SESSION_TOKEN"',
        "mcp_servers.taikun_plan.required=true",
    ]
    return token, overrides


def _runner_record(values: dict[str, str], *, workspace: str, status: str) -> dict[str, Any]:
    personal_bound = str(values.get("personal_bound") or "").strip() == "1"
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
            "auth_lane": (
                "chatgpt_personal_host_local" if personal_bound
                else "codex_host_local"
            ),
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


def _write_watch_output(chunk: bytes, stream: Any = None) -> None:
    """Copy native Codex bytes into the supervisor-owned PTY immediately."""
    if not chunk:
        return
    target = stream
    if target is None:
        target = getattr(sys.stdout, "buffer", sys.stdout)
    try:
        target.write(chunk)
    except TypeError:
        target.write(chunk.decode("utf-8", errors="replace"))
    target.flush()


def _run_streaming_command(
    command: list[str], *, cwd: str, env: dict[str, str], timeout: float,
    stream: Any = None,
) -> subprocess.CompletedProcess:
    """Run a child while teeing its combined output to the browser Watch PTY.

    ``subprocess.run(..., stdout=PIPE)`` withheld the native Codex transcript
    until the process exited. The Agent Host supervisor already owns the outer
    PTY, so copying each ready pipe chunk to stdout makes that exact transcript
    observable while retaining the same bytes for completion evidence.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    if process.stdout is None:  # pragma: no cover - guaranteed by stdout=PIPE
        process.kill()
        raise RuntimeError("native Codex output pipe was not created")

    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _mask in selector.select(min(0.25, remaining)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                _write_watch_output(chunk, stream)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout, output=bytes(output))
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        try:
            tail, _unused = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            tail = b""
        if tail:
            output.extend(tail)
            _write_watch_output(tail, stream)
        raise subprocess.TimeoutExpired(
            command, timeout, output=bytes(output)) from exc
    finally:
        selector.close()
        process.stdout.close()

    rendered = bytes(output).decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(command, returncode, rendered, "")


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
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
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
    personal_bound = bool(account_binding)
    wake_id = str(os.environ.get("PM_CO_WAKE_ID") or "").strip()
    runner_session_id = str(os.environ.get("PM_RUNNER_SESSION_ID") or "").strip()
    values = {
        "task_id": str(task.get("task_id") or "").strip(),
        "claim_id": str(task.get("claim_id") or task.get("id") or "").strip(),
        "work_session_id": str(managed.get("work_session_id") or "").strip(),
        "workspace": str(managed.get("workspace_path") or "").strip(),
        "host_id": str(os.environ.get("PM_CO_HOST_ID")
                       or os.environ.get("PM_HOST_ID") or "").strip(),
        "runner_session_id": runner_session_id,
        "wake_id": wake_id,
        "source_sha": str(
            os.environ.get("PM_SOURCE_SHA") or managed.get("head_sha") or "").strip(),
        "execution_connection_id": str(
            os.environ.get("PM_EXECUTION_CONNECTION_ID")
            or (f"host-local:{wake_id}:{runner_session_id}"
                if wake_id and runner_session_id else "")).strip(),
        "agent_id": str(os.environ.get("PM_AGENT_ID") or "").strip(),
        "personal_bound": "1" if personal_bound else "0",
    }
    missing = sorted(
        key for key, value in values.items()
        if key != "personal_bound" and not value)
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
    mismatches = (sorted(
        key for key, value in relational.items()
        if str(value or "").strip() != relational_expected[key]
    ) if personal_bound else [])
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
    for key in _METERED_PROVIDER_ENV | _COORDINATION_CREDENTIAL_ENV:
        environment.pop(key, None)
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
        mcp_overrides: list[str] = []
        if personal_bound:
            child_token, mcp_overrides = _work_session_mcp_bootstrap(http, values)
            environment["SWITCHBOARD_WORK_SESSION_TOKEN"] = child_token
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
            *[value for override in mcp_overrides for value in ("-c", override)],
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
        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            name=f"switchboard-heartbeat-{values['runner_session_id']}",
            daemon=True,
        )
        heartbeat_thread.start()
        if runner is None:
            completed = _run_streaming_command(
                command, cwd=workspace, env=environment, timeout=7200)
        else:
            # Test and embedding hook. Production deliberately takes the
            # streaming path above; injected runners retain the previous
            # CompletedProcess contract.
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
                "host_coordination_credential_exported": False,
                "metered_api_key_paths_absent": True,
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "output_bytes": len(output),
                "provider_output_redacted": True,
                "runner_heartbeat_errors_recovered": len(heartbeat_errors),
            },
        }
        lifecycle_lock = threading.Lock()
        lifecycle_state = {"terminal": ""}

        if not personal_bound:
            def finalize_host_local(
                    succeeded: bool, reason: str = "",
                    postprocessing_evidence: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                del postprocessing_evidence
                with lifecycle_lock:
                    desired = "completed" if succeeded else "failed"
                    if lifecycle_state["terminal"] == desired:
                        return {"status": desired, "idempotent": True}
                    stop_heartbeat.set()
                    if heartbeat_thread is not None:
                        heartbeat_thread.join()
                    _update_runner(
                        http, values, workspace=workspace, status=desired)
                    # Generic host-local wakes acknowledge that the native
                    # process launched; they are not task-outcome receipts.
                    # A later executed-test/checkpoint failure must therefore
                    # terminalize the runner and let the outer loop abandon the
                    # claim, but must not rewrite the already-completed launch
                    # wake. Personal exact-host wakes use ``finalize`` below
                    # and retain the narrow completed -> failed recovery edge.
                    lifecycle_state["terminal"] = desired
                    return {"status": desired}

            evidence[_PERSONAL_EXECUTION_LIFECYCLE_KEY] = {
                "complete": lambda postprocessing_evidence: finalize_host_local(
                    True, postprocessing_evidence=postprocessing_evidence),
                "fail": lambda reason="": finalize_host_local(False, reason),
            }
            return evidence

        recovery_path = _recovery_path(values)

        def finalize(succeeded: bool, reason: str = "",
                     postprocessing_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
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
                    durable_evidence = dict(postprocessing_evidence or evidence)
                    _write_recovery_receipt(
                        recovery_path, values, managed, durable_evidence,
                        "ready_to_terminalize")
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
                    _write_recovery_receipt(
                        recovery_path, values, managed, durable_evidence,
                        "terminalized")
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
                if not succeeded:
                    recovery_path.unlink(missing_ok=True)
                return result

        def checkpointed(postprocessing_evidence: dict[str, Any],
                         _checkpoint: dict[str, Any]) -> None:
            _write_recovery_receipt(
                recovery_path, values, managed, postprocessing_evidence,
                "checkpointed")

        def claim_completed(_postprocessing_evidence: dict[str, Any],
                            _completion: dict[str, Any]) -> None:
            receipt = _load_recovery_receipt(recovery_path)
            receipt["stage"] = "completed"
            receipt["updated_at"] = time.time()
            _atomic_recovery_json(recovery_path, receipt)

        def cleanup_completed() -> None:
            receipt = _load_recovery_receipt(recovery_path)
            if receipt.get("stage") != "completed":
                raise RuntimeError("personal recovery cleanup preceded claim completion")
            recovery_path.unlink(missing_ok=True)

        evidence[_PERSONAL_EXECUTION_LIFECYCLE_KEY] = {
            "complete": lambda postprocessing_evidence: finalize(
                True, postprocessing_evidence=postprocessing_evidence),
            "fail": lambda reason="": finalize(False, reason),
            "checkpointed": checkpointed,
            "claim_completed": claim_completed,
            "cleanup_completed": cleanup_completed,
        }
        return evidence
    except Exception:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join()
        if runner_registered and not wake_completed:
            try:
                # Publish the terminal runner first so the repository can
                # distinguish a real execution failure from a still-running
                # worker. Only personal exact-host wakes represent the full
                # execution outcome and can use the narrow completed -> failed
                # recovery receipt; generic wakes already durably acknowledged
                # process launch and must not be rewritten here.
                _update_runner(
                    http, values, workspace=workspace, status="failed")
                if personal_bound:
                    _complete_wake(http, values, {
                        "started": False,
                        "reason": "native_codex_execution_failed",
                        "task_id": values["task_id"],
                        "recoverable_post_execution_failure": True,
                    })
                wake_completed = True
            except Exception:
                # The terminal runner tuple is durable and the identical wake receipt
                # remains safe to retry after an outcome-unknown response.
                raise
        raise


def _resume_recovery_receipt(
        path: Path, *, http: Callable[..., dict[str, Any]] = sb._http,
        base: str | None = None, token: str | None = None) -> dict[str, Any]:
    receipt = _load_recovery_receipt(path)
    binding = dict(receipt.get("binding") or {})
    managed = dict(receipt.get("managed") or {})
    evidence = dict(receipt.get("evidence") or {})
    project = str(receipt.get("project") or "").strip()
    values = {**binding, "workspace": str(managed.get("workspace_path") or "")}
    required = {
        "project": project,
        "task_id": str(receipt.get("task_id") or ""),
        "claim_id": str(receipt.get("claim_id") or ""),
        "agent_id": str(receipt.get("agent_id") or ""),
        "workspace": values["workspace"],
        "head_sha": str(evidence.get("head_sha") or ""),
    }
    if not all(required.values()) or not all(str(value or "").strip()
                                              for value in binding.values()):
        raise RuntimeError("personal recovery receipt binding is incomplete")
    if (project != str(os.environ.get("PM_PROJECT") or "switchboard").strip()
            or binding.get("host_id")
            != str(os.environ.get("PM_HOST_ID") or "").strip()):
        raise RuntimeError("personal recovery receipt does not belong to this host")
    stage = str(receipt.get("stage") or "")
    # Claim completion is already durable at this stage. Cleanup is explicitly
    # absent-safe, so a crash after deleting the checkout must not wedge recovery
    # merely because there is no longer a checkout whose HEAD can be inspected.
    if stage == "completed":
        cleanup = sb._cleanup_personal_bound_workspace(managed)
        if not cleanup.get("cleaned"):
            return {
                "recovered": False, "stage": "completed",
                "cleanup": cleanup, "path": str(path),
            }
        path.unlink(missing_ok=True)
        return {"recovered": True, "stage": "completed"}
    if stage in {"recovery_expired", "recovery_quarantined"}:
        if stage == "recovery_expired":
            receipt["stage"] = "recovery_quarantined"
            receipt["quarantined_at"] = time.time()
            receipt["quarantine_reason"] = "automatic_recovery_deadline_expired"
            receipt["updated_at"] = time.time()
            _atomic_recovery_json(path, receipt)
        return {
            "recovered": False, "quarantined": True,
            "stage": "recovery_quarantined", "path": str(path),
        }
    deadline = float(receipt.get("recovery_deadline") or 0)
    if deadline <= time.time():
        # Stop blocking unrelated work after the bounded automatic-recovery window,
        # while retaining the exact receipt and checkout for operator disposition.
        receipt["stage"] = "recovery_quarantined"
        receipt["quarantined_at"] = time.time()
        receipt["quarantine_reason"] = "automatic_recovery_deadline_expired"
        receipt["updated_at"] = time.time()
        _atomic_recovery_json(path, receipt)
        return {
            "recovered": False, "quarantined": True,
            "stage": "recovery_quarantined", "path": str(path),
        }
    workspace = values["workspace"]
    if (not Path(workspace).is_dir()
            or _git(workspace, "rev-parse", "HEAD") != evidence.get("head_sha")
            or _git(workspace, "status", "--porcelain")):
        raise RuntimeError("personal recovery checkout no longer matches its exact head")

    if stage in {"ready_to_terminalize", "terminalizing"}:
        receipt["stage"] = "terminalizing"
        receipt["updated_at"] = time.time()
        _atomic_recovery_json(path, receipt)
        _update_runner(http, values, workspace=workspace, status="completed")
        _complete_wake(http, values, dict(receipt.get("terminal_result") or {}))
        receipt["stage"] = "terminalized"
        receipt["updated_at"] = time.time()
        _atomic_recovery_json(path, receipt)
        stage = "terminalized"

    if stage == "terminalized":
        checkpoint = sb.checkpoint_personal_work_session_with_recovery(
            project, managed, evidence, receipt["agent_id"],
            base=base, token=token, binding=binding)
        if not checkpoint.get("updated"):
            return {
                "recovered": False, "stage": stage,
                "checkpoint": checkpoint, "path": str(path),
            }
        receipt["stage"] = "checkpointed"
        receipt["updated_at"] = time.time()
        _atomic_recovery_json(path, receipt)
        stage = "checkpointed"

    if stage == "checkpointed":
        completion = sb.complete_personal_claim_with_recovery(
            project, receipt["task_id"], receipt["claim_id"], managed,
            evidence, receipt["agent_id"], base=base, token=token,
            binding=binding)
        if not completion.get("completed"):
            return {
                "recovered": False, "stage": stage,
                "completion": completion, "path": str(path),
            }
        receipt["stage"] = "completed"
        receipt["updated_at"] = time.time()
        _atomic_recovery_json(path, receipt)
        cleanup = sb._cleanup_personal_bound_workspace(managed)
        if not cleanup.get("cleaned"):
            return {
                "recovered": False, "stage": "completed",
                "cleanup": cleanup, "path": str(path),
            }
        path.unlink(missing_ok=True)
        return {
            "recovered": True, "stage": "completed",
            "task_id": receipt["task_id"],
            "claim_id": receipt["claim_id"],
        }
    raise RuntimeError(f"personal recovery receipt has unsupported stage {stage!r}")


def resume_pending_postprocessing(
        *, http: Callable[..., dict[str, Any]] = sb._http,
        base: str | None = None, token: str | None = None) -> dict[str, Any]:
    """Resume every durable local checkpoint/claim completion before new work."""
    root = _recovery_root()
    recovered: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            result = _resume_recovery_receipt(
                path, http=http, base=base, token=token)
            if result.get("recovered"):
                recovered.append(result)
            elif result.get("quarantined"):
                quarantined.append(result)
            else:
                pending.append(result)
        except Exception as exc:
            pending.append({"path": str(path), "error": str(exc)})
    return {
        "schema": "switchboard.personal_postprocessing_recovery_scan.v1",
        "recovered": recovered,
        "pending": pending,
        "quarantined": quarantined,
        "recovered_count": len(recovered),
        "pending_count": len(pending),
        "quarantined_count": len(quarantined),
    }


__all__ = ["run", "resume_pending_postprocessing"]
