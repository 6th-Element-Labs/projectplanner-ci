#!/usr/bin/env python3
"""Claude Code cloud transport for the ADAPTER-17 cloud-execution contract.

The supported provider trigger is the Claude Code CLI's ``--cloud`` bridge.  The
CLI intentionally requires a TTY, so this module allocates a pseudo-terminal,
captures only bounded/redacted output, and adopts a run only after a
``claude.ai/code/session_...`` URL can be read back.

Credentials are deliberately outside this process.  A cloud environment must
provide ``SWITCHBOARD_TOKEN`` and the repository's ``.mcp.json`` must reference
that variable.  Raw tokens are rejected before the provider is called.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from adapters.cloud_execution import (
    CANONICAL_REPO,
    evaluate_trigger,
    load_contract,
    validate_dispatch_envelope,
)


VENDOR_ID = "claude-code-cloud"
MIN_CLI_VERSION = (2, 1, 195)
TOKEN_ENV = "SWITCHBOARD_TOKEN"
TOKEN_REF = f"provider-env://{TOKEN_ENV}"
SESSION_URL_RE = re.compile(
    r"https://claude\.ai/code/(session_[A-Za-z0-9_-]+)"
)
ANSI_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
LITERAL_BEARER_RE = re.compile(r"Bearer\s+(?!\$\{)[A-Za-z0-9._~+/-]{16,}")
VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")


def strip_terminal_control(text: str) -> str:
    """Remove ANSI control sequences while preserving printable provider text."""
    return ANSI_RE.sub("", text or "").replace("\r", "")


def parse_cli_version(text: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.search(text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def session_receipt_from_output(output: str, exit_code: int = 0) -> dict[str, Any]:
    """Turn bounded CLI output into a provider receipt without retaining the prompt."""
    clean = strip_terminal_control(output)
    # TUI links may encode the URL only in an OSC-8 hyperlink target. Search the
    # raw frame first, then the printable text, before discarding terminal control.
    urls = SESSION_URL_RE.findall(output or "") or SESSION_URL_RE.findall(clean)
    if urls:
        path_id = urls[-1]
        suffix = path_id.removeprefix("session_")
        return {
            "ok": True,
            "session_id": f"cse_{suffix}",
            "session_url": f"https://claude.ai/code/{path_id}",
            "status": "running",
            "output_hash": "sha256:" + hashlib.sha256(clean.encode("utf-8")).hexdigest(),
        }
    lowered = clean.lower()
    if "requires an interactive terminal" in lowered:
        reason = "claude_cloud_tty_required"
    elif "require a claude.ai login" in lowered or "run /login" in lowered:
        reason = "claude_cloud_auth_required"
    elif "session creation failed" in lowered:
        reason = "claude_cloud_session_creation_failed"
    elif exit_code:
        reason = "claude_cloud_cli_failed"
    else:
        reason = "adoption_receipt_incomplete"
    return {
        "ok": False,
        "error": reason,
        "exit_code": int(exit_code),
        "output_hash": "sha256:" + hashlib.sha256(clean.encode("utf-8")).hexdigest(),
    }


def _default_run(command: list[str], cwd: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _git_repo_name(remote: str) -> str:
    value = (remote or "").strip().removesuffix(".git")
    if ":" in value and not value.startswith(("http://", "https://")):
        value = value.split(":", 1)[1]
    elif "/" in value:
        value = value.split("github.com/", 1)[-1]
    return value.strip("/")


def validate_project_mcp_config(repo_root: str | Path) -> list[str]:
    """Require provider-side secret expansion and reject any committed bearer value."""
    path = Path(repo_root) / ".mcp.json"
    if not path.is_file():
        return ["project_mcp_config_missing"]
    raw = path.read_text(encoding="utf-8")
    if LITERAL_BEARER_RE.search(raw):
        return ["project_mcp_config_contains_literal_bearer"]
    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        return ["project_mcp_config_malformed"]
    server = ((config.get("mcpServers") or {}).get("taikun-plan") or {})
    auth = ((server.get("headers") or {}).get("Authorization") or "").strip()
    errors: list[str] = []
    if server.get("type") != "http" or server.get("url") != "https://plan.taikunai.com/mcp":
        errors.append("project_mcp_endpoint_invalid")
    if auth != f"Bearer ${{{TOKEN_ENV}}}":
        errors.append("project_mcp_token_must_use_provider_secret")
    return errors


def preflight_environment(
    dispatch: dict[str, Any],
    repo_root: str | Path,
    *,
    run: Callable[..., subprocess.CompletedProcess] = _default_run,
    claude_path: str | None = None,
) -> dict[str, Any]:
    """Prove everything observable before paying for a provider launch."""
    errors = validate_dispatch_envelope(dispatch)
    root = str(Path(repo_root).resolve())
    cli = claude_path or shutil.which("claude")
    version: tuple[int, int, int] | None = None
    auth: dict[str, Any] = {}
    head_sha = ""
    if not cli:
        errors.append("claude_cli_missing")
    else:
        try:
            version_run = run([cli, "--version"], cwd=root, timeout=15)
            version = parse_cli_version((version_run.stdout or "") + (version_run.stderr or ""))
            if version is None or version < MIN_CLI_VERSION:
                errors.append("claude_cli_cloud_version_unsupported")
        except (OSError, subprocess.SubprocessError):
            errors.append("claude_cli_version_unavailable")
        try:
            auth_run = run([cli, "auth", "status", "--json"], cwd=root, timeout=15)
            auth = json.loads(auth_run.stdout or "{}")
            if not auth.get("loggedIn") or auth.get("authMethod") != "claude.ai":
                errors.append("claude_cloud_subscription_auth_required")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            errors.append("claude_cloud_auth_status_unavailable")

    def git(*args: str) -> subprocess.CompletedProcess:
        return run(["git", *args], cwd=root, timeout=20)

    try:
        remote = (git("remote", "get-url", "origin").stdout or "").strip()
        if _git_repo_name(remote) != CANONICAL_REPO:
            errors.append("canonical_repo_remote_mismatch")
        branch = (git("branch", "--show-current").stdout or "").strip()
        if branch != dispatch.get("branch"):
            errors.append("dispatch_branch_not_checked_out")
        if git("status", "--porcelain").stdout.strip():
            errors.append("dispatch_worktree_dirty")
        head_sha = (git("rev-parse", "HEAD").stdout or "").strip()
        pushed = git("ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}")
        remote_sha = ((pushed.stdout or "").split() or [""])[0]
        if pushed.returncode != 0 or not head_sha or remote_sha != head_sha:
            errors.append("dispatch_branch_not_pushed_exact")
        ancestry = git("merge-base", "--is-ancestor", "origin/master", "HEAD")
        if ancestry.returncode != 0:
            errors.append("dispatch_branch_not_based_on_current_master")
    except (OSError, subprocess.SubprocessError):
        errors.append("git_preflight_unavailable")

    errors.extend(validate_project_mcp_config(root))
    if dispatch.get("mcp_access", {}).get("token_ref") != TOKEN_REF:
        errors.append("scoped_mcp_token_ref_not_provider_bound")
    return {
        "ok": not errors,
        "vendor_id": VENDOR_ID,
        "errors": sorted(set(errors)),
        "claude_cli": cli or "",
        "claude_cli_version": ".".join(str(part) for part in version) if version else "",
        "auth_method": auth.get("authMethod") or "",
        "subscription_type": auth.get("subscriptionType") or "",
        "branch": dispatch.get("branch") or "",
        "head_sha": head_sha,
    }


class PtyCloudLauncher:
    """Launch ``claude --cloud`` with the interactive terminal it requires."""

    def __init__(self, claude_path: str = "claude", timeout_s: float = 120,
                 max_output_bytes: int = 131_072):
        self.claude_path = claude_path
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes

    def launch(self, prompt: str, cwd: str | Path) -> dict[str, Any]:
        master, slave = pty.openpty()
        started = time.monotonic()
        process: subprocess.Popen | None = None
        chunks: list[bytes] = []
        size = 0
        try:
            process = subprocess.Popen(
                [self.claude_path, "--cloud", prompt],
                cwd=str(cwd),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                env={**os.environ, "TERM": os.environ.get("TERM") or "xterm-256color"},
            )
            os.close(slave)
            slave = -1
            deadline = started + self.timeout_s
            while True:
                if time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    receipt = session_receipt_from_output(b"".join(chunks).decode("utf-8", "replace"), 124)
                    receipt["error"] = "claude_cloud_launch_timeout"
                    return receipt
                readable, _, _ = select.select([master], [], [], 0.25)
                if readable:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        data = b""
                    if data and size < self.max_output_bytes:
                        keep = data[: self.max_output_bytes - size]
                        chunks.append(keep)
                        size += len(keep)
                if process.poll() is not None:
                    # Drain the final terminal frame.
                    try:
                        while True:
                            data = os.read(master, 4096)
                            if not data:
                                break
                            if size < self.max_output_bytes:
                                keep = data[: self.max_output_bytes - size]
                                chunks.append(keep)
                                size += len(keep)
                    except OSError:
                        pass
                    break
            receipt = session_receipt_from_output(
                b"".join(chunks).decode("utf-8", "replace"), process.returncode or 0
            )
            receipt["duration_s"] = round(time.monotonic() - started, 3)
            return receipt
        finally:
            if slave >= 0:
                os.close(slave)
            os.close(master)
            if process is not None and process.poll() is None:
                process.kill()


class ReceiptStore:
    """Small host-local idempotency store; receipts contain no provider or MCP secret."""

    def __init__(self, root: str | Path | None = None):
        default = Path.home() / ".cache" / "switchboard" / "claude-cloud-receipts"
        self.root = Path(root or os.environ.get("PM_CLAUDE_CLOUD_RECEIPTS") or default)

    def _path(self, wake_id: str) -> Path:
        key = hashlib.sha256(wake_id.encode("utf-8")).hexdigest()
        return self.root / f"{key}.json"

    def get(self, wake_id: str) -> dict[str, Any] | None:
        path = self._path(wake_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def put(self, wake_id: str, receipt: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(wake_id)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)


class ClaudeCloudAdapter:
    vendor_id = VENDOR_ID

    def __init__(self, repo_root: str | Path, *, launcher: Any | None = None,
                 receipt_store: ReceiptStore | None = None,
                 contract: dict[str, Any] | None = None,
                 preflight_fn: Callable[..., dict[str, Any]] = preflight_environment):
        self.repo_root = Path(repo_root).resolve()
        self.launcher = launcher or PtyCloudLauncher()
        self.receipts = receipt_store or ReceiptStore()
        self.contract = contract or load_contract()
        self.preflight_fn = preflight_fn

    def preflight(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        return self.preflight_fn(dispatch, self.repo_root)

    def trigger(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        existing = self.receipts.get(str(dispatch.get("wake_id") or ""))
        if existing:
            return {**existing, "idempotent_replay": True}
        preflight = self.preflight(dispatch)
        if not preflight.get("ok"):
            return {"ok": False, "error": "claude_cloud_preflight_failed",
                    "preflight": preflight}
        readiness = evaluate_trigger(
            self.vendor_id,
            dispatch,
            {
                "claude_subscription_auth",
                "claude_cloud_enabled",
                "github_repo_grant",
                "pushed_base_branch",
                "project_mcp_config",
                "scoped_mcp_token_ref",
            },
            active_sessions=int(dispatch.get("active_sessions") or 0),
            contract=self.contract,
        )
        if not readiness.get("allowed"):
            return {"ok": False, "error": readiness.get("reason"), "evaluation": readiness}
        provider = self.launcher.launch(str(dispatch.get("dev_brief") or ""), self.repo_root)
        adopted = evaluate_trigger(
            self.vendor_id,
            dispatch,
            {
                "claude_subscription_auth",
                "claude_cloud_enabled",
                "github_repo_grant",
                "pushed_base_branch",
                "project_mcp_config",
                "scoped_mcp_token_ref",
            },
            active_sessions=int(dispatch.get("active_sessions") or 0),
            provider_result=provider,
            contract=self.contract,
        )
        if not adopted.get("adopted"):
            return {"ok": False, "error": adopted.get("reason"),
                    "provider_error": provider.get("error"), "evaluation": adopted}
        receipt = {"ok": True, **provider, **adopted, "preflight": preflight}
        self.receipts.put(dispatch["wake_id"], receipt)
        return receipt

    def get_session(self, provider_session_id: str) -> dict[str, Any]:
        for path in self.receipts.root.glob("*.json") if self.receipts.root.exists() else []:
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if receipt.get("provider_session_id") == provider_session_id:
                return {"ok": True, "session_id": provider_session_id,
                        "session_url": receipt.get("session_url"), "status": "running"}
        return {"ok": False, "error": "provider_session_unknown",
                "session_id": provider_session_id}
