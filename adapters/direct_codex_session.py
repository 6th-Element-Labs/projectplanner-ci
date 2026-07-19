#!/usr/bin/env python3
"""Start one browser-visible native Codex CLI from a direct Switchboard assignment.

The Agent Host has already been selected by the signed-in operator.  This helper
does only the local boot work: persist the non-secret assignment TOML, prepare an
isolated task worktree, preload the enrolled host's Switchboard MCP connection,
and replace itself with the native interactive Codex CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import urllib.parse
import urllib.request


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _toml_string(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=True)


def _assignment() -> dict[str, Any]:
    raw = str(os.environ.get("PM_DIRECT_CODEX_ASSIGNMENT_JSON") or "").strip()
    if not raw:
        raise RuntimeError("direct Codex assignment is missing")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != "switchboard.direct_cli_assignment.v1":
        raise RuntimeError("direct Codex assignment schema is invalid")
    for key in ("project", "task_id", "host_id", "prompt"):
        if not str(value.get(key) or "").strip():
            raise RuntimeError(f"direct Codex assignment missing {key}")
    if not _ID_RE.fullmatch(str(value["task_id"])):
        raise RuntimeError("direct Codex task id is invalid")
    return value


def _run_git(source: Path, *args: str, timeout: int = 600) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), *args], capture_output=True, text=True,
        timeout=timeout, check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "git failed")[-1000:].strip()
        raise RuntimeError(detail)
    return (completed.stdout or "").strip()


def _prepare_workspace(assignment: dict[str, Any]) -> tuple[Path, str, str]:
    source_raw = str(os.environ.get("PM_AGENT_HOST_SOURCE_REPO_ROOT") or "").strip()
    source = Path(source_raw).expanduser().resolve() if source_raw else None
    if not source or not source.is_dir() or source.is_symlink():
        raise RuntimeError("enrolled source repository is unavailable")
    if _run_git(source, "rev-parse", "--is-inside-work-tree") != "true":
        raise RuntimeError("enrolled source repository is not a git worktree")

    repo = assignment.get("repository") or {}
    default_branch = str(repo.get("default_branch") or "master").strip()
    if subprocess.run(
        ["git", "check-ref-format", "--branch", default_branch],
        capture_output=True, text=True, check=False,
    ).returncode:
        raise RuntimeError("assignment default branch is invalid")

    print(f"Switchboard assigned {assignment['task_id']} to {assignment['host_id']}", flush=True)
    print(f"Repository: {source}", flush=True)
    print(f"Preparing isolated workspace from origin/{default_branch}...", flush=True)
    _run_git(source, "fetch", "--prune", "origin")
    canonical_sha = _run_git(source, "rev-parse", f"origin/{default_branch}")
    expected_sha = str(repo.get("canonical_sha") or "").strip()
    if expected_sha and (not _SHA_RE.fullmatch(expected_sha) or expected_sha != canonical_sha):
        print(
            f"Canonical head advanced from {expected_sha[:12]} to {canonical_sha[:12]}; "
            "using the fetched canonical head.", flush=True,
        )

    runner_id = str(os.environ.get("PM_RUNNER_SESSION_ID") or "direct")
    suffix = hashlib.sha256(
        f"{assignment['project']}:{assignment['task_id']}:{runner_id}".encode()
    ).hexdigest()[:10]
    workspace_root_raw = str(
        os.environ.get("PM_PERSONAL_WORKSPACE_ROOT")
        or os.environ.get("PM_WORKSPACE_ROOT")
        or source.parent / "switchboard-agent-workspaces"
    )
    workspace_root = Path(workspace_root_raw).expanduser().resolve()
    if workspace_root in {Path("/"), Path.home()} or workspace_root.is_symlink():
        raise RuntimeError("personal workspace root is unsafe")
    workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    task_marker = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(assignment["task_id"]).lower())
    workspace = (workspace_root / f"{task_marker}-{suffix}").resolve()
    if workspace.parent != workspace_root:
        raise RuntimeError("direct Codex workspace escaped its root")
    # The assignment carries the task's branch prefix, not a globally reusable
    # branch.  Every runner gets its own deterministic suffix because a failed
    # session may leave its branch attached to the old worktree.  Reusing
    # ``codex/<task>`` in a new worktree would make the retry fail before Codex
    # starts with "branch is already checked out".
    branch_base = str(
        repo.get("branch") or f"codex/{str(assignment['task_id']).lower()}"
    ).rstrip("-/")
    branch = f"{branch_base}-direct-{suffix}"
    if subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        capture_output=True, text=True, check=False,
    ).returncode:
        raise RuntimeError("assignment task branch is invalid")

    if not workspace.exists():
        _run_git(source, "worktree", "add", "-b", branch, str(workspace), canonical_sha, timeout=180)
    else:
        if workspace.is_symlink() or _run_git(workspace, "rev-parse", "--is-inside-work-tree") != "true":
            raise RuntimeError("existing direct Codex workspace is invalid")
        if _run_git(workspace, "status", "--porcelain"):
            raise RuntimeError("existing direct Codex workspace is dirty")
    return workspace, branch, canonical_sha


def _write_assignment_toml(
    assignment: dict[str, Any], workspace: Path, branch: str, canonical_sha: str,
) -> Path:
    runner_id = str(os.environ.get("PM_RUNNER_SESSION_ID") or "direct")
    runner_root_raw = str(
        os.environ.get("PM_AGENT_HOST_RUNNER_DIR")
        or os.environ.get("PM_RUNNER_DIR")
        or ".switchboard/runner"
    )
    runner_root = Path(runner_root_raw).expanduser().resolve()
    session_root = (runner_root / runner_id).resolve()
    if session_root.parent != runner_root or not session_root.is_dir() or session_root.is_symlink():
        raise RuntimeError("direct Codex runner directory is invalid")
    repo = assignment.get("repository") or {}
    mcp = assignment.get("mcp") or {}
    values = [
        'schema = "switchboard.direct_cli_assignment.v1"',
        f"project = {_toml_string(assignment.get('project'))}",
        f"task_id = {_toml_string(assignment.get('task_id'))}",
        f"deliverable_id = {_toml_string(assignment.get('deliverable_id'))}",
        f"host_id = {_toml_string(assignment.get('host_id'))}",
        f"runner_session_id = {_toml_string(runner_id)}",
        f"wake_id = {_toml_string(os.environ.get('PM_CO_WAKE_ID'))}",
        f"prompt = {_toml_string(assignment.get('prompt'))}",
        "",
        "[repository]",
        f"slug = {_toml_string(repo.get('slug'))}",
        f"source_root = {_toml_string(os.environ.get('PM_AGENT_HOST_SOURCE_REPO_ROOT'))}",
        f"workspace = {_toml_string(workspace)}",
        f"default_branch = {_toml_string(repo.get('default_branch') or 'master')}",
        f"branch = {_toml_string(branch)}",
        f"canonical_sha = {_toml_string(canonical_sha)}",
        "",
        "[mcp]",
        f"endpoint = {_toml_string(mcp.get('endpoint'))}",
        'auth_source = "enrolled_agent_host_token"',
        "secret_persisted = false",
        "",
    ]
    path = session_root / "assignment.toml"
    temporary = session_root / "assignment.toml.tmp"
    temporary.write_text("\n".join(values), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return path


def _issue_direct_mcp_token(assignment: dict[str, Any]) -> str:
    base = str(os.environ.get("PM_BASE") or "https://plan.taikunai.com").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Switchboard base URL is invalid")
    host_token = str(os.environ.get("PM_MCP_TOKEN") or "").strip()
    if not host_token:
        raise RuntimeError("enrolled Agent Host token is unavailable")
    body = json.dumps({
        "project": assignment["project"],
        "wake_id": str(os.environ.get("PM_CO_WAKE_ID") or ""),
        "host_id": assignment["host_id"],
        "runner_session_id": str(os.environ.get("PM_RUNNER_SESSION_ID") or ""),
    }, sort_keys=True).encode()
    request = urllib.request.Request(
        base + "/ixp/v1/direct_assignments/mcp_token",
        data=body,
        headers={
            "Authorization": f"Bearer {host_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"direct Switchboard MCP authentication failed: {exc}") from exc
    token = str(result.get("token") or "").strip()
    if result.get("issued") is not True or not token.startswith("dst-"):
        raise RuntimeError("direct Switchboard MCP authentication was denied")
    return token


def main() -> int:
    assignment = _assignment()
    workspace, branch, canonical_sha = _prepare_workspace(assignment)
    assignment_path = _write_assignment_toml(assignment, workspace, branch, canonical_sha)
    print(f"Assignment config: {assignment_path}", flush=True)
    print(f"Task branch: {branch} ({canonical_sha[:12]})", flush=True)
    print("Connecting this CLI to Switchboard MCP...", flush=True)
    direct_token = _issue_direct_mcp_token(assignment)
    print(f"Starting native Codex CLI with: {assignment['prompt']}", flush=True)

    endpoint = str((assignment.get("mcp") or {}).get("endpoint") or "").strip()
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Switchboard MCP endpoint is invalid")
    environment = os.environ.copy()
    environment["SWITCHBOARD_DIRECT_SESSION_TOKEN"] = direct_token
    executable = str(os.environ.get("PM_CODEX_EXECUTABLE") or "codex").strip()
    command = [
        executable,
        "-C", str(workspace),
        "--dangerously-bypass-approvals-and-sandbox",
        "-c", f"mcp_servers.taikun_plan.url={json.dumps(endpoint)}",
        "-c", 'mcp_servers.taikun_plan.bearer_token_env_var="SWITCHBOARD_DIRECT_SESSION_TOKEN"',
        "-c", "mcp_servers.taikun_plan.required=true",
        str(assignment["prompt"]),
    ]
    os.execvpe(executable, command, environment)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Direct Codex launch failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
