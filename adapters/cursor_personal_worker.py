"""Cursor personal-account worker with one fenced Switchboard credential lease.

The worker mirrors the Claude/Codex BYOA lifecycle: bind the runner, acquire and
admit one account-scoped lease, materialize the key only inside an isolated runtime,
run the real Cursor Agent binary, publish redacted evidence, then purge and release.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

try:
    import switchboard_core as sb
except ModuleNotFoundError:  # package import in tests and library callers
    from adapters import switchboard_core as sb
from switchboard.integrations.provider_runtime_auth import ProviderRuntimeAuth
from switchboard.integrations.worker_credential_envelope import decrypt_on_worker


_FORBIDDEN_METERED_KEYS = (
    "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
)


def _binding() -> dict[str, Any]:
    try:
        value = json.loads(os.environ.get("PM_CO_ACCOUNT_BINDING_JSON", "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("CO account binding is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("CO account binding is invalid")
    return value


def _git(workspace: str, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", workspace, *args], capture_output=True, text=True,
        timeout=30, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed")
    return (completed.stdout or "").strip()


def _account_attribution(provider: str, provider_account_id: str) -> str:
    digest = hashlib.sha256(f"{provider}\x1f{provider_account_id}".encode()).hexdigest()
    return f"acct-{digest[:16]}"


def _lease_body(binding: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    managed = task.get("managed") or {}
    values = {
        "project": binding.get("project") or os.environ.get("PM_PROJECT", "switchboard"),
        "credential_reference": binding.get("credential_reference"),
        "user_id": binding.get("user_id"),
        "provider": binding.get("provider"),
        "provider_account_id": binding.get("provider_account_id"),
        "task_id": task.get("task_id"),
        "host_id": os.environ.get("PM_CO_HOST_ID") or os.environ.get("PM_HOST_ID"),
        "runner_session_id": os.environ.get("PM_RUNNER_SESSION_ID"),
        "work_session_id": managed.get("work_session_id"),
        "account_affinity_id": binding.get("account_affinity_id"),
        "ttl_seconds": 900,
    }
    if not all(values.get(key) for key in (
            "project", "credential_reference", "user_id", "provider",
            "provider_account_id", "task_id", "host_id", "runner_session_id",
            "work_session_id", "account_affinity_id")):
        raise RuntimeError("CO runtime binding is incomplete")
    if values["provider"] != "cursor":
        raise RuntimeError("Cursor worker requires provider=cursor")
    return values


def _runner_body(task: dict[str, Any], body: dict[str, Any], status: str) -> dict[str, Any]:
    claim_id = str(task.get("claim_id") or task.get("id") or "")
    runtime_binding = _binding()
    return {
        "project": body["project"],
        "runner_session_id": body["runner_session_id"],
        "host_id": body["host_id"],
        "agent_id": os.environ.get("PM_AGENT_ID"),
        "runtime": "cursor-agent",
        "task_id": body["task_id"],
        "claim_id": claim_id,
        "status": status,
        "cwd": (task.get("managed") or {}).get("workspace_path"),
        "control": {"tier": "T3", "runner_kill": True, "managed_process": True},
        "metadata": {
            "tenant_id": runtime_binding.get("tenant_id"),
            "user_id": body["user_id"],
            "project": body["project"],
            "task_id": body["task_id"],
            "host_id": body["host_id"],
            "runner_session_id": body["runner_session_id"],
            "claim_id": claim_id,
            "wake_id": os.environ.get("PM_CO_WAKE_ID"),
            "work_session_id": body["work_session_id"],
            "credential_reference": body["credential_reference"],
            "provider_account_id": body["provider_account_id"],
            "provider_account_attribution": _account_attribution(
                body["provider"], body["provider_account_id"]),
            "provider": body["provider"],
            "account_affinity_id": body["account_affinity_id"],
            "credential_admission_phase": "claim_bound",
            "auth_lane": "cursor_personal",
            **({"terminal_reason": f"personal_subscription_worker_{status}"}
               if status in {"completed", "failed"} else {}),
        },
        "heartbeat_ttl_s": 1800,
    }


def _register_runner(task: dict[str, Any], body: dict[str, Any], status: str) -> None:
    result = sb._http("POST", "/ixp/v1/register_runner_session",
                      _runner_body(task, body, status))
    if not result or result.get("error"):
        raise RuntimeError("runner binding update failed")


def _refuse_forbidden_keys(env: dict[str, str]) -> None:
    present = [key for key in _FORBIDDEN_METERED_KEYS if env.get(key)]
    if present:
        raise RuntimeError("unrelated metered provider paths must be absent: " + ",".join(present))


def _cursor_binary(path: str = "") -> str:
    if path:
        return path
    return shutil.which("cursor-agent") or shutil.which("agent") or "cursor-agent"


def _smoke_command() -> list[str]:
    raw = str(os.environ.get("PM_CURSOR_SMOKE_COMMAND") or "").strip()
    return shlex.split(raw) if raw else [_cursor_binary(), "--version"]


def _redacted_binding(body: dict[str, Any], claim_id: str, lease_id: str) -> dict[str, Any]:
    binding = _binding()
    return {
        "tenant_id": binding.get("tenant_id"),
        "user_id": body["user_id"],
        "provider": body["provider"],
        "provider_account_id": body["provider_account_id"],
        "provider_account_attribution": _account_attribution(
            body["provider"], body["provider_account_id"]),
        "credential_reference": body["credential_reference"],
        "credential_lease_id": lease_id,
        "project": body["project"],
        "task_id": body["task_id"],
        "host_id": body["host_id"],
        "runner_session_id": body["runner_session_id"],
        "work_session_id": body["work_session_id"],
        "claim_id": claim_id,
        "credential_values_redacted": True,
    }


def run(task: dict[str, Any]) -> dict[str, Any]:
    binding = _binding()
    lease_body = _lease_body(binding, task)
    project = lease_body["project"]
    claim_id = str(task.get("claim_id") or task.get("id") or "")
    wake_id = str(os.environ.get("PM_CO_WAKE_ID") or "")
    workspace = str((task.get("managed") or {}).get("workspace_path") or os.getcwd())
    lease_id = ""
    runtime_root: Path | None = None
    credential = ""
    provider_env: dict[str, str] = {}
    wake_completed = False
    runner_registered = False
    try:
        _register_runner(task, lease_body, "running")
        runner_registered = True
        lease = sb._http(
            "POST",
            f"/api/projects/{project}/provider-connections/"
            f"{lease_body['credential_reference']}/leases",
            lease_body,
            timeout=30,
        )
        lease_id = str(lease.get("lease_id") or "")
        if not lease_id:
            raise RuntimeError("provider credential lease acquisition failed")

        admitted = sb._http("POST", "/txp/v1/claim_wake", {
            "project": project,
            "host_id": lease_body["host_id"],
            "wake_id": wake_id,
            "runner_session_id": lease_body["runner_session_id"],
            "credential_lease_id": lease_id,
            "claim_id": claim_id,
            "work_session_id": lease_body["work_session_id"],
        })
        if not admitted.get("claimed") or admitted.get("credential_admission_phase") != "ready":
            raise RuntimeError("wake credential admission was denied")

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        envelope = sb._http(
            "POST",
            f"/api/projects/{project}/provider-credential-leases/{lease_id}/materialize-envelope",
            {**lease_body, "public_key_pem": public_pem},
            timeout=30,
        )
        credential = decrypt_on_worker(envelope, private_pem)
        private_pem = b""
        private_key = None

        runtime = ProviderRuntimeAuth(
            base_environment=os.environ,
            cli_paths={"cursor": _cursor_binary()},
        )
        provider = str(lease_body["provider"])
        runtime_root = runtime._runtime_root(provider)
        provider_env, _state = runtime._materialize(provider, credential, runtime_root)
        _refuse_forbidden_keys(provider_env)
        preflight = runtime._preflight(provider, provider_env, workspace)
        credential = ""
        if not preflight.get("authenticated") or preflight.get("auth_mode") != "personal_api_key":
            raise RuntimeError("Cursor personal-account preflight failed")

        activated = sb._http(
            "POST",
            f"/api/projects/{project}/provider-credential-leases/{lease_id}/activate",
            lease_body,
            timeout=30,
        )
        if activated.get("state") != "active":
            raise RuntimeError("provider credential lease activation failed")

        completed = subprocess.run(
            _smoke_command(), cwd=workspace, env=provider_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120, check=False,
        )
        _refuse_forbidden_keys(provider_env)
        provider_env.pop("CURSOR_API_KEY", None)
        if completed.returncode != 0 or not (
                (completed.stdout or "").strip() or (completed.stderr or "").strip()):
            raise RuntimeError("Cursor personal-account smoke failed")
        output = ((completed.stdout or "") + (completed.stderr or "")).encode()
        branch = _git(workspace, "branch", "--show-current")
        head_sha = _git(workspace, "rev-parse", "HEAD")
        if runtime_root:
            purged_root = runtime_root
            shutil.rmtree(purged_root, ignore_errors=True)
            runtime_root = None
            if purged_root.exists():
                raise RuntimeError("Cursor runtime residue purge failed")
        sb._http("POST", "/txp/v1/complete_wake", {
            "project": project,
            "wake_id": wake_id,
            "runner_session_id": lease_body["runner_session_id"],
            "agent_id": os.environ.get("PM_AGENT_ID"),
            "result": {
                "started": True,
                "reason": "cursor_personal_account_smoke_completed",
                "provider": provider,
                "auth_mode": preflight.get("auth_mode"),
                "credential_values_redacted": True,
                "metered_fallback": False,
            },
        })
        wake_completed = True
        return {
            "branch": branch,
            "head_sha": head_sha,
            "git_diff_check": "clean" if not _git(workspace, "status", "--porcelain") else "dirty",
            "verification": {
                "schema": "switchboard.cursor_personal_smoke.v1",
                "started": True,
                "provider": provider,
                "auth_mode": preflight.get("auth_mode"),
                "personal_subscription": True,
                "api_key_fallback": False,
                "metered_fallback": False,
                "provider_output_redacted": True,
                "credential_values_redacted": True,
                "residue_purged": True,
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "output_bytes": len(output),
                "provider_account_attribution": _account_attribution(
                    provider, lease_body["provider_account_id"]),
                "binding": _redacted_binding(lease_body, claim_id, lease_id),
            },
        }
    finally:
        credential = ""
        provider_env.pop("CURSOR_API_KEY", None)
        if runtime_root:
            shutil.rmtree(runtime_root, ignore_errors=True)
        if lease_id:
            try:
                sb._http(
                    "POST",
                    f"/api/projects/{project}/provider-credential-leases/{lease_id}/release",
                    {"project": project, "reason": "provider_runtime_exit"},
                    timeout=30,
                )
            except Exception:
                pass
        if not wake_completed:
            try:
                sb._http("POST", "/txp/v1/complete_wake", {
                    "project": project,
                    "wake_id": wake_id,
                    "runner_session_id": lease_body["runner_session_id"],
                    "agent_id": os.environ.get("PM_AGENT_ID"),
                    "result": {
                        "started": False,
                        "reason": "cursor_personal_account_runtime_failed",
                        "credential_values_redacted": True,
                    },
                })
            except Exception:
                pass
        if runner_registered:
            try:
                _register_runner(task, lease_body, "completed" if wake_completed else "failed")
            except Exception:
                pass


__all__ = ["run"]
