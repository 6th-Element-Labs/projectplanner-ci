#!/usr/bin/env python3
"""OpenAI Codex cloud adapter for the ADAPTER-17 cloud-execution contract.

The official Codex CLI is the transport.  ``codex cloud exec`` performs the
short-lived outbound trigger while the coding task runs in an OpenAI-managed
environment.  This module never falls back to ``codex exec`` or App Server,
because those execute on caller-controlled compute and are not cloud receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
    validate_usage_receipt,
)


VENDOR_ID = "openai-codex-cloud"
TASK_URL_RE = re.compile(r"https://chatgpt\.com/codex/tasks/([^/?#\s]+)")
REQUIRED_SETUP = {
    "codex_cli_authenticated",
    "codex_cloud_environment_id",
    "github_repo_grant",
    "scoped_mcp_environment_bridge",
    "agent_internet_plan_taikunai_com",
}
TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _safe_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[-limit:]


def _deny(reason: str, failure_class: str, **detail: Any) -> dict[str, Any]:
    return {
        "allowed": False,
        "adopted": False,
        "dev_status": "failed",
        "reason": reason,
        "failure_class": failure_class,
        **detail,
    }


def _repo_slug(remote: str) -> str:
    value = str(remote or "").strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return value.split(":", 1)[1]
    marker = "github.com/"
    return value.split(marker, 1)[1] if marker in value else value


def build_cloud_prompt(dispatch: dict[str, Any]) -> str:
    """Build the provider prompt without expanding the opaque MCP token reference."""
    mcp = dispatch.get("mcp_access") or {}
    return "\n".join(
        [
            f"Work Switchboard task {dispatch['task_id']} via project=switchboard.",
            dispatch["dev_brief"].strip(),
            f"Canonical repository: {dispatch['canonical_repo']}",
            f"Required task branch: {dispatch['branch']} (never main or master).",
            f"Switchboard MCP endpoint: {mcp['endpoint']}",
            f"Use the preconfigured scoped credential reference: {mcp['token_ref']}",
            "Start with the Switchboard handshake, keep the claim/work-session evidence current, "
            "run the repository tests, push the task branch, and open a pull request.",
            "Do not merge. Report branch, head SHA, PR URL, tests, and honest usage back through "
            "Switchboard. If MCP, repository, network, or PR access is unavailable, fail visibly "
            "and do not substitute local or unrelated compute.",
        ]
    )


class CodexCloudAdapter:
    vendor_id = VENDOR_ID

    def __init__(
        self,
        *,
        environment_id: str = "",
        cli_path: str = "codex",
        repo_path: str | Path = ".",
        attempts: int = 1,
        timeout_seconds: int = 60,
        mcp_environment_bridge: bool | None = None,
        agent_internet_enabled: bool | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.environment_id = (
            environment_id or os.environ.get("PM_CODEX_CLOUD_ENVIRONMENT_ID", "")
        ).strip()
        self.cli_path = cli_path or os.environ.get("PM_CODEX_CLI", "codex")
        self.repo_path = str(repo_path)
        self.attempts = max(1, min(4, int(attempts or 1)))
        self.timeout_seconds = max(5, int(timeout_seconds or 60))
        self.mcp_environment_bridge = (
            _truthy(os.environ.get("PM_CODEX_CLOUD_MCP_CONFIGURED"))
            if mcp_environment_bridge is None
            else bool(mcp_environment_bridge)
        )
        self.agent_internet_enabled = (
            _truthy(os.environ.get("PM_CODEX_CLOUD_AGENT_INTERNET"))
            if agent_internet_enabled is None
            else bool(agent_internet_enabled)
        )
        self._runner = runner
        self._sleep = sleep
        self._cli_version = ""

    def _run(self, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        return self._runner(
            args,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout or self.timeout_seconds,
            check=False,
        )

    def _provider_failure(self, proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        detail = _safe_text("\n".join(part for part in (proc.stdout, proc.stderr) if part))
        lowered = detail.lower()
        if "not signed in" in lowered:
            return _deny(
                "missing_provider_setup",
                "absent_permission",
                vendor_id=self.vendor_id,
                missing=["codex_cli_authenticated"],
                provider_error=detail,
            )
        if any(
            token in lowered
            for token in (
                "no cloud environments are available",
                "environment '",
                "environment not found",
                "repo_not_accessible",
                "repository is not accessible",
            )
        ):
            return _deny(
                "missing_provider_setup",
                "absent_permission",
                vendor_id=self.vendor_id,
                missing=["codex_cloud_environment_id", "github_repo_grant"],
                provider_error=detail,
            )
        return _deny(
            "vendor_api_error",
            "broken_connection",
            vendor_id=self.vendor_id,
            provider_error=detail or f"codex exited {proc.returncode}",
        )

    def preflight(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        errors = validate_dispatch_envelope(dispatch)
        if errors:
            return _deny("invalid_dispatch_envelope", "invalid_input", errors=errors)
        if not self.environment_id:
            return _deny(
                "missing_provider_setup",
                "absent_permission",
                vendor_id=self.vendor_id,
                missing=["codex_cloud_environment_id"],
            )
        if not self.mcp_environment_bridge:
            return _deny(
                "missing_provider_setup",
                "absent_permission",
                vendor_id=self.vendor_id,
                missing=["scoped_mcp_environment_bridge"],
                detail=(
                    "Codex cloud secrets are setup-only. The selected environment must expose "
                    "a preconfigured, scoped MCP bridge without putting a raw token in the prompt."
                ),
            )
        if not self.agent_internet_enabled:
            return _deny(
                "missing_provider_setup",
                "absent_permission",
                vendor_id=self.vendor_id,
                missing=["agent_internet_plan_taikunai_com"],
            )

        resolved = self.cli_path if os.path.isabs(self.cli_path) else shutil.which(self.cli_path)
        if not resolved:
            return _deny(
                "missing_provider_setup",
                "missing_data",
                vendor_id=self.vendor_id,
                missing=["codex_cli"],
            )
        version = self._run([resolved, "--version"], timeout=10)
        if version.returncode != 0:
            return self._provider_failure(version)
        self._cli_version = _safe_text(version.stdout or version.stderr, 200)

        remote = self._run(["git", "remote", "get-url", "origin"], timeout=10)
        if remote.returncode != 0 or _repo_slug(remote.stdout) != CANONICAL_REPO:
            return _deny(
                "missing_provider_setup",
                "wrong_repo",
                vendor_id=self.vendor_id,
                missing=["github_repo_grant"],
                expected_repo=CANONICAL_REPO,
                observed_repo=_repo_slug(remote.stdout),
            )

        listed = self._run(
            [resolved, "cloud", "list", "--env", self.environment_id, "--json", "--limit", "1"]
        )
        if listed.returncode != 0:
            return self._provider_failure(listed)
        try:
            payload = json.loads(listed.stdout or "{}")
        except json.JSONDecodeError:
            return _deny(
                "provider_response_malformed",
                "malformed_payload",
                vendor_id=self.vendor_id,
                provider_error=_safe_text(listed.stdout),
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks", []), list):
            return _deny("provider_response_malformed", "malformed_payload", vendor_id=self.vendor_id)
        return {
            "allowed": True,
            "vendor_id": self.vendor_id,
            "environment_id": self.environment_id,
            "cli_path": resolved,
            "cli_version": self._cli_version,
            "requirements": sorted(REQUIRED_SETUP),
        }

    def trigger(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        ready = self.preflight(dispatch)
        if not ready.get("allowed"):
            return ready
        prompt = build_cloud_prompt(dispatch)
        proc = self._run(
            [
                ready["cli_path"],
                "cloud",
                "exec",
                "--env",
                self.environment_id,
                "--attempts",
                str(self.attempts),
                "--branch",
                dispatch["branch"],
                prompt,
            ],
            timeout=self.timeout_seconds,
        )
        if proc.returncode != 0:
            return self._provider_failure(proc)
        match = TASK_URL_RE.search(proc.stdout or "")
        if not match:
            return _deny(
                "adoption_receipt_incomplete",
                "missing_data",
                vendor_id=self.vendor_id,
                missing=["task_id", "task_url"],
                provider_output=_safe_text(proc.stdout),
            )
        task_url = match.group(0)
        return {
            "ok": True,
            "vendor_id": self.vendor_id,
            "task_id": match.group(1),
            "task_url": task_url,
            "status": "pending",
            "environment_id": self.environment_id,
            "cli_version": self._cli_version,
            "prompt_hash": "sha256:" + hashlib.sha256(prompt.encode()).hexdigest(),
        }

    def get_session(self, provider_session_id: str) -> dict[str, Any]:
        resolved = self.cli_path if os.path.isabs(self.cli_path) else shutil.which(self.cli_path)
        if not resolved or not self.environment_id:
            return _deny("missing_provider_setup", "absent_permission", vendor_id=self.vendor_id)
        proc = self._run(
            [resolved, "cloud", "list", "--env", self.environment_id, "--json", "--limit", "20"]
        )
        if proc.returncode != 0:
            return self._provider_failure(proc)
        try:
            tasks = json.loads(proc.stdout or "{}").get("tasks") or []
        except (AttributeError, json.JSONDecodeError):
            return _deny("provider_response_malformed", "malformed_payload", vendor_id=self.vendor_id)
        row = next((item for item in tasks if str(item.get("id")) == provider_session_id), None)
        if not row:
            return _deny(
                "vendor_session_lost",
                "unreachable_agent",
                vendor_id=self.vendor_id,
                provider_session_id=provider_session_id,
            )
        return {
            "ok": True,
            "task_id": str(row.get("id")),
            "task_url": str(row.get("url") or f"https://chatgpt.com/codex/tasks/{provider_session_id}"),
            "status": str(row.get("status") or "").lower(),
            "environment_id": row.get("environment_id") or self.environment_id,
            "environment_label": row.get("environment_label"),
            "summary": row.get("summary") or {},
        }

    def launch(self, dispatch: dict[str, Any], active_sessions: int = 0) -> dict[str, Any]:
        ready = self.preflight(dispatch)
        if not ready.get("allowed"):
            return ready
        contract = load_contract()
        gate = evaluate_trigger(
            self.vendor_id,
            dispatch,
            ready["requirements"],
            active_sessions,
            contract=contract,
        )
        if not gate.get("allowed"):
            return gate
        created = self.trigger(dispatch)
        if not created.get("ok"):
            return created
        provider_id = created["task_id"]
        readback: dict[str, Any] = {}
        for attempt in range(3):
            readback = self.get_session(provider_id)
            if readback.get("ok"):
                break
            if attempt < 2:
                self._sleep(1.0)
        if not readback.get("ok"):
            return readback
        adopted = evaluate_trigger(
            self.vendor_id,
            dispatch,
            ready["requirements"],
            active_sessions,
            provider_result=readback,
            contract=contract,
        )
        if adopted.get("adopted"):
            adopted.update(
                {
                    "environment_id": self.environment_id,
                    "cli_version": self._cli_version,
                    "prompt_hash": created["prompt_hash"],
                }
            )
        return adopted


def usage_receipt(binding: dict[str, Any]) -> dict[str, Any]:
    receipt = {
        "source": "agent_report",
        "confidence": "unknown",
        "billing_mode": "subscription",
        "cost_usd": 0,
        "task_id": binding.get("task_id"),
        "vendor_id": VENDOR_ID,
        "provider_session_id": binding.get("provider_session_id"),
        "runner_session_id": binding.get("runner_session_id"),
        "recorded_at": time.time(),
        "note": "Codex cloud did not expose task-level tokens or cost in the CLI receipt.",
    }
    errors = validate_usage_receipt(receipt)
    if errors:
        raise ValueError("invalid Codex cloud usage receipt: " + "; ".join(errors))
    return receipt


def launch_wake(
    wake: dict[str, Any],
    inventory: dict[str, Any],
    *,
    active_sessions: int = 0,
    adapter: CodexCloudAdapter | None = None,
) -> dict[str, Any]:
    """Launch one explicit Codex cloud wake and return a runner-session-shaped receipt."""
    policy = wake.get("policy") or {}
    cloud = policy.get("cloud_execution") or policy
    task_id = str(wake.get("task_id") or "").upper()
    branch = str(cloud.get("branch") or f"codex/{task_id.lower()}")
    mcp_access = dict(cloud.get("mcp_access") or {})
    mcp_access.setdefault("endpoint", os.environ.get("PM_MCP_PUBLIC_URL", "https://plan.taikunai.com/mcp"))
    mcp_access.setdefault(
        "token_ref",
        os.environ.get("PM_CODEX_CLOUD_MCP_TOKEN_REF", f"switchboard://scoped-token/{task_id}"),
    )
    mcp_access.setdefault("scopes", ["read:task", "write:claim", "write:evidence"])
    mcp_access.setdefault("expires_at", time.time() + 3600)
    dispatch = {
        "schema": "switchboard.cloud_dispatch.v1",
        "project": wake.get("project") or inventory.get("project") or "switchboard",
        "task_id": task_id,
        "claim_id": cloud.get("claim_id") or "",
        "wake_id": wake.get("wake_id") or "",
        "dev_brief": cloud.get("dev_brief") or wake.get("reason") or f"Implement {task_id}.",
        "canonical_repo": cloud.get("canonical_repo") or CANONICAL_REPO,
        "branch": branch,
        "continuity": "fresh_only",
        "mcp_access": mcp_access,
    }
    adapter = adapter or CodexCloudAdapter(repo_path=inventory.get("repo_root") or ".")
    binding = adapter.launch(dispatch, active_sessions=active_sessions)
    if not binding.get("adopted"):
        return {
            "started": False,
            "cloud_session": True,
            "wake_mode": "cloud_execution",
            "task_id": task_id,
            **binding,
        }
    rec = {
        "started": True,
        "cloud_session": True,
        "wake_mode": "cloud_execution",
        "runner_session_id": binding["runner_session_id"],
        "provider_session_id": binding["provider_session_id"],
        "session_url": binding["session_url"],
        "agent_id": (wake.get("selector") or {}).get("agent_id") or f"codex/{task_id}",
        "runtime": "codex",
        "task_id": task_id,
        "claim_id": binding.get("claim_id") or "",
        "status": "running",
        "cwd": "",
        "control": {
            "tier": "T1",
            "managed_process": False,
            "runner_kill": False,
            "runner_open": False,
            "vendor_managed": True,
        },
        "metadata": {
            "wake_id": wake.get("wake_id"),
            "vendor_id": VENDOR_ID,
            "provider_session_id": binding["provider_session_id"],
            "session_url": binding["session_url"],
            "environment_id": binding.get("environment_id"),
            "cli_version": binding.get("cli_version"),
            "prompt_hash": binding.get("prompt_hash"),
            "billing_mode": "subscription",
        },
    }
    rec["usage_receipt"] = usage_receipt(rec | binding)
    return rec
