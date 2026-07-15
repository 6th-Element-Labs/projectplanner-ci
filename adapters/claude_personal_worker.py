"""CO Fleet work function for one real, personal-subscription Claude smoke."""
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
        "host_id": os.environ.get("PM_CO_HOST_ID"),
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
    result = sb._http("POST", "/ixp/v1/register_runner_session", {
        "project": body["project"],
        "runner_session_id": body["runner_session_id"],
        "host_id": body["host_id"],
        "agent_id": os.environ.get("PM_AGENT_ID"),
        "runtime": "claude-code",
        "task_id": body["task_id"],
        "claim_id": task.get("claim_id") or task.get("id"),
        "status": "running",
        "cwd": (task.get("managed") or {}).get("workspace_path"),
        "control": {"tier": "T3", "runner_kill": True, "managed_process": True},
        "metadata": {
            "wake_id": os.environ.get("PM_CO_WAKE_ID"),
            "work_session_id": body["work_session_id"],
            "credential_reference": body["credential_reference"],
            "provider_account_id": body["provider_account_id"],
            "provider": body["provider"],
            "account_affinity_id": body["account_affinity_id"],
            "credential_admission_phase": "claim_bound",
        },
        "heartbeat_ttl_s": 1800,
    })
    if not result or result.get("error"):
        raise RuntimeError("runner claim binding failed")


def _terminalize_bound_runner(
    task: dict[str, Any], body: dict[str, Any], *, succeeded: bool,
) -> None:
    """Persist a terminal central row after the supervised worker exits.

    The runner registry is an audited drain input, so process exit must not leave it
    advertising ``running`` merely because an exception bypassed the happy path.
    """
    result = sb._http("POST", "/ixp/v1/register_runner_session", {
        "project": body["project"],
        "runner_session_id": body["runner_session_id"],
        "host_id": body["host_id"],
        "agent_id": os.environ.get("PM_AGENT_ID"),
        "runtime": "claude-code",
        "task_id": body["task_id"],
        "claim_id": task.get("claim_id") or task.get("id"),
        "cwd": (task.get("managed") or {}).get("workspace_path"),
        "status": "completed" if succeeded else "failed",
        "control": {"tier": "T3", "runner_kill": True, "managed_process": True},
        "metadata": {
            "wake_id": os.environ.get("PM_CO_WAKE_ID"),
            "work_session_id": body["work_session_id"],
            "credential_reference": body["credential_reference"],
            "provider_account_id": body["provider_account_id"],
            "provider": body["provider"],
            "account_affinity_id": body["account_affinity_id"],
            "credential_admission_phase": "claim_bound",
            "terminal_reason": (
                "personal_subscription_worker_completed" if succeeded
                else "personal_subscription_worker_failed"
            ),
        },
        "heartbeat_ttl_s": 1800,
    })
    if not result or result.get("error"):
        raise RuntimeError("runner terminal state update failed")


def _safe_preflight_evidence(preflight: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "error_code", "attempt_count", "exit_code", "failure_kind",
        "stdout_bytes", "stderr_bytes", "stdout_sha256", "stderr_sha256",
        "provider_output_redacted",
    )
    return {
        f"preflight_{key}": preflight[key]
        for key in allowed if key in preflight
    }


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
    preflight: dict[str, Any] = {}
    wake_completed = False
    runner_registered = False
    try:
        _register_bound_runner(task, lease_body)
        runner_registered = True
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
        preflight = runtime._preflight(provider, provider_env, workspace)
        credential = ""
        if not preflight.get("authenticated") or preflight.get("auth_mode") != "oauth_personal":
            raise RuntimeError("Claude personal-subscription preflight failed")

        activated = sb._http(
            "POST",
            f"/api/projects/{project}/provider-credential-leases/{lease_id}/activate",
            lease_body,
            timeout=30,
        )
        if activated.get("state") != "active":
            raise RuntimeError("provider credential lease activation failed")

        prompt = (
            "You are the first CO Fleet BYOA smoke. Inspect the current repository and "
            "return one compact JSON object with keys task_id, repository_kind, and smoke_ok. "
            f"Set task_id to {lease_body['task_id']} and smoke_ok to true. Do not edit files."
        )
        completed = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            cwd=workspace,
            env=provider_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
            check=False,
        )
        provider_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        if completed.returncode != 0 or not (completed.stdout or "").strip():
            raise RuntimeError("Claude personal-subscription smoke failed")
        output = (completed.stdout or "").encode()
        branch = _git(workspace, "branch", "--show-current")
        head_sha = _git(workspace, "rev-parse", "HEAD")
        sb._http("POST", "/txp/v1/complete_wake", {
            "project": project,
            "wake_id": wake_id,
            "runner_session_id": lease_body["runner_session_id"],
            "agent_id": os.environ.get("PM_AGENT_ID"),
            "result": {
                "started": True,
                "reason": "personal_subscription_smoke_completed",
                "provider": provider,
                "auth_mode": preflight.get("auth_mode"),
                "credential_values_redacted": True,
            },
        })
        wake_completed = True
        return {
            "branch": branch,
            "head_sha": head_sha,
            "git_diff_check": "clean" if not _git(workspace, "status", "--porcelain") else "dirty",
            "verification": {
                "schema": "switchboard.co_byoa_smoke.v1",
                "provider": provider,
                "auth_mode": preflight.get("auth_mode"),
                "personal_subscription": True,
                "api_key_fallback": False,
                "wake_id": wake_id,
                "host_id": lease_body["host_id"],
                "runner_session_id": lease_body["runner_session_id"],
                "work_session_id": lease_body["work_session_id"],
                "claim_id": claim_id,
                "lease_id": lease_id,
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "output_bytes": len(output),
                "provider_output_redacted": True,
            },
        }
    finally:
        credential = ""
        provider_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
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
                        "reason": "personal_subscription_runtime_failed",
                        "credential_values_redacted": True,
                        **_safe_preflight_evidence(preflight),
                    },
                })
            except Exception:
                pass
        if runner_registered:
            try:
                _terminalize_bound_runner(task, lease_body, succeeded=wake_completed)
            except Exception:
                pass


__all__ = ["run"]
