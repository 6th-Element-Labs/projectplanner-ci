"""CO Fleet / dedicated-host work function for Codex personal-subscription smoke.

Mirrors adapters/claude_personal_worker.py for the CO-11 path:
claim → register_runner_session(task_id+claim_id+host_id) → exclusive credential lease →
ChatGPT personal preflight → refuse metered API keys → short Codex smoke → purge/release.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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

# Align with co_fleet.py forbidden personal-subscription fallback fields.
_METERED_API_KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")


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
    return values


def _register_bound_runner(task: dict[str, Any], body: dict[str, Any]) -> None:
    claim_id = str(task.get("claim_id") or task.get("id") or "")
    result = sb._http("POST", "/ixp/v1/register_runner_session", {
        "project": body["project"],
        "runner_session_id": body["runner_session_id"],
        "host_id": body["host_id"],
        "agent_id": os.environ.get("PM_AGENT_ID"),
        "runtime": "codex",
        "task_id": body["task_id"],
        "claim_id": claim_id,
        "status": "running",
        "cwd": (task.get("managed") or {}).get("workspace_path"),
        "control": {"tier": "T3", "runner_kill": True, "managed_process": True,
                    "runner_open": True, "runner_inject": True, "runner_logs": True},
        "metadata": {
            "wake_id": os.environ.get("PM_CO_WAKE_ID"),
            "work_session_id": body["work_session_id"],
            "credential_reference": body["credential_reference"],
            "provider_account_id": body["provider_account_id"],
            "provider": body["provider"],
            "account_affinity_id": body["account_affinity_id"],
            "credential_admission_phase": "claim_bound",
            "auth_lane": "chatgpt_personal",
        },
        "heartbeat_ttl_s": 1800,
    })
    if not result or result.get("error"):
        raise RuntimeError("runner claim binding failed")
    if not (result.get("task_id") or body["task_id"]):
        raise RuntimeError("runner session missing task_id")
    if not claim_id:
        raise RuntimeError("runner session missing claim_id")
    if not (result.get("host_id") or body["host_id"]):
        raise RuntimeError("runner session missing host_id")


def _refuse_metered_keys(env: dict[str, str]) -> None:
    present = [key for key in _METERED_API_KEYS if env.get(key)]
    if present:
        raise RuntimeError(
            "metered Codex/OpenAI API key paths must be absent: " + ",".join(present)
        )


def _smoke_command() -> list[str]:
    raw = str(os.environ.get("PM_CODEX_SMOKE_COMMAND") or "codex --version").strip()
    if not raw:
        return ["codex", "--version"]
    return raw.split()


def run(task: dict[str, Any]) -> dict[str, Any]:
    binding = _binding()
    lease_body = _lease_body(binding, task)
    project = lease_body["project"]
    claim_id = str(task.get("claim_id") or task.get("id") or "")
    wake_id = str(os.environ.get("PM_CO_WAKE_ID") or "")
    workspace = str((task.get("managed") or {}).get("workspace_path") or os.getcwd())
    reference = lease_body["credential_reference"]
    lease_id = ""
    runtime_root: Path | None = None
    credential = ""
    provider_env: dict[str, str] = {}
    wake_completed = False
    try:
        _register_bound_runner(task, lease_body)
        lease = sb._http(
            "POST",
            f"/api/projects/{project}/provider-connections/{reference}/leases",
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

        runtime = ProviderRuntimeAuth(base_environment=os.environ)
        provider = str(lease_body["provider"])
        runtime_root = runtime._runtime_root(provider)
        provider_env, _state = runtime._materialize(provider, credential, runtime_root)
        _refuse_metered_keys(provider_env)
        preflight = runtime._preflight(provider, provider_env, workspace)
        credential = ""
        if not preflight.get("authenticated") or preflight.get("auth_mode") != "chatgpt_personal":
            raise RuntimeError("Codex personal-subscription preflight failed")

        activated = sb._http(
            "POST",
            f"/api/projects/{project}/provider-credential-leases/{lease_id}/activate",
            lease_body,
            timeout=30,
        )
        if activated.get("state") != "active":
            raise RuntimeError("provider credential lease activation failed")

        completed = subprocess.run(
            _smoke_command(),
            cwd=workspace,
            env=provider_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        _refuse_metered_keys(provider_env)
        if completed.returncode != 0 or not (
                (completed.stdout or "").strip() or (completed.stderr or "").strip()):
            raise RuntimeError("Codex personal-subscription smoke failed")
        output = ((completed.stdout or "") + (completed.stderr or "")).encode()
        branch = _git(workspace, "branch", "--show-current")
        head_sha = _git(workspace, "rev-parse", "HEAD")
        sb._http("POST", "/txp/v1/complete_wake", {
            "project": project,
            "wake_id": wake_id,
            "runner_session_id": lease_body["runner_session_id"],
            "agent_id": os.environ.get("PM_AGENT_ID"),
            "result": {
                "started": True,
                "reason": "codex_personal_subscription_smoke_completed",
                "provider": provider,
                "auth_mode": preflight.get("auth_mode"),
                "credential_values_redacted": True,
                "metered_api_key_paths_absent": True,
            },
        })
        wake_completed = True
        return {
            "branch": branch,
            "head_sha": head_sha,
            "git_diff_check": "clean" if not _git(workspace, "status", "--porcelain") else "dirty",
            "verification": {
                "schema": "switchboard.co11_codex_personal_smoke.v1",
                "provider": provider,
                "auth_mode": preflight.get("auth_mode"),
                "personal_subscription": True,
                "api_key_fallback": False,
                "metered_api_key_paths_absent": True,
                "wake_id": wake_id,
                "host_id": lease_body["host_id"],
                "runner_session_id": lease_body["runner_session_id"],
                "work_session_id": lease_body["work_session_id"],
                "task_id": lease_body["task_id"],
                "claim_id": claim_id,
                "lease_id": lease_id,
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "output_bytes": len(output),
                "provider_output_redacted": True,
            },
        }
    finally:
        credential = ""
        for key in _METERED_API_KEYS:
            provider_env.pop(key, None)
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
                        "reason": "codex_personal_subscription_runtime_failed",
                        "credential_values_redacted": True,
                    },
                })
            except Exception:
                pass


__all__ = ["run"]
