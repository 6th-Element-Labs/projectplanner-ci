"""Personal-subscription CLI authentication inside one CO-6 credential lease.

The integration owns the provider process boundary: it restores credentials only in an
isolated runtime home/environment, runs the vendor's local authentication preflight, starts
one lane, performs fenced Codex auth-state writeback, and purges the runtime before release.
No raw provider output or credential value enters its receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from switchboard.application.commands.provider_credentials import (
    start_with_provider_credential,
)
from switchboard.domain.provider_credentials import (
    CredentialPolicyError,
    CredentialPrincipal,
    normalize_provider,
)
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    ProviderCredentialRepository,
    default_provider_credential_repository,
)


PROVIDER_RUNTIME_RECEIPT_SCHEMA = "switchboard.provider_runtime_auth.receipt.v1"
MAX_CODEX_CAPSULE_BYTES = 1024 * 1024

_PROVIDER_CLI = {
    "openai-codex": "codex",
    "anthropic-claude": "claude",
    "cursor": "cursor-agent",
}
_PROVIDER_CLI_CANDIDATES = {
    # Cursor renamed the standalone binary from ``cursor-agent`` to ``agent``.
    # Fleet images may carry either name while they roll forward, so discovery is
    # explicit and bounded instead of silently falling back to an unrelated CLI.
    "cursor": ("cursor-agent", "agent"),
}
_PROVIDER_PREFLIGHT = {
    "openai-codex": ("login", "status"),
    "anthropic-claude": ("auth", "status", "--json"),
    "cursor": ("status", "--format", "json"),
}
_PROVIDER_SECRET_KEYS = {
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CURSOR_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "PM_PROVIDER_VAULT_KEY",
    "PM_SESSION_SECRET",
    "PM_API_KEYS",
}


def _account_fingerprint(provider: str, account_id: str) -> str:
    digest = hashlib.sha256(f"{provider}\x1f{account_id}".encode()).hexdigest()
    return f"acct-{digest[:16]}"


def _safe_json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _truthy_field(payload: Mapping[str, Any], *names: str) -> bool:
    for name in names:
        if name not in payload:
            continue
        value = payload.get(name)
        if isinstance(value, bool):
            return value
        if str(value or "").strip().lower() in {"1", "true", "yes", "authenticated", "logged_in"}:
            return True
    return False


class ProviderRuntimeAuth:
    """Run one provider CLI process using an exact CO-6 materialization lease."""

    def __init__(
        self,
        *,
        repository: ProviderCredentialRepository = default_provider_credential_repository,
        runtime_parent: str | Path | None = None,
        cli_paths: Mapping[str, str] | None = None,
        command_runner: Callable[..., Any] = subprocess.run,
        process_factory: Callable[..., Any] = subprocess.Popen,
        preflight_timeout_seconds: int = 20,
        preflight_attempts: int = 3,
        preflight_retry_delay_seconds: float = 0.5,
        sleep_fn: Callable[[float], None] = time.sleep,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.repository = repository
        self.runtime_parent = Path(
            runtime_parent or os.environ.get("PM_PROVIDER_RUNTIME_ROOT")
            or (Path(tempfile.gettempdir()) / "switchboard-provider-runtimes")
        )
        self.cli_paths = dict(cli_paths or {})
        self.command_runner = command_runner
        self.process_factory = process_factory
        self.preflight_timeout_seconds = max(2, int(preflight_timeout_seconds))
        self.preflight_attempts = max(1, min(5, int(preflight_attempts)))
        self.preflight_retry_delay_seconds = max(
            0.0, min(5.0, float(preflight_retry_delay_seconds)))
        self.sleep_fn = sleep_fn
        self.base_environment = dict(
            os.environ if base_environment is None else base_environment)

    def _runtime_root(self, provider: str) -> Path:
        self.runtime_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_stat = self.runtime_parent.lstat()
        if self.runtime_parent.is_symlink() or not stat.S_ISDIR(parent_stat.st_mode):
            raise CredentialVaultError(
                "provider_runtime_root_invalid", "provider runtime root is invalid", status_code=409)
        os.chmod(self.runtime_parent, 0o700)
        path = Path(tempfile.mkdtemp(
            prefix=f"switchboard-{provider}-", dir=str(self.runtime_parent)))
        os.chmod(path, 0o700)
        return path

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(path, 0o700)

    @staticmethod
    def _secure_write(path: Path, value: str) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            data = value.encode("utf-8")
            if not data or len(data) > MAX_CODEX_CAPSULE_BYTES:
                raise CredentialVaultError(
                    "credential_materialization_failed",
                    "Codex auth capsule has an invalid size",
                    status_code=409,
                )
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(path, 0o600)

    @staticmethod
    def _read_secure_codex_capsule(path: Path) -> str:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 \
                    or info.st_size > MAX_CODEX_CAPSULE_BYTES:
                raise CredentialVaultError(
                    "credential_writeback_invalid",
                    "Codex auth capsule writeback is invalid",
                    status_code=409,
                )
            chunks: list[bytes] = []
            remaining = info.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining:
                raise CredentialVaultError(
                    "credential_writeback_invalid",
                    "Codex auth capsule writeback is invalid",
                    status_code=409,
                )
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialVaultError(
                "credential_writeback_invalid",
                "Codex auth capsule writeback is invalid",
                status_code=409,
            ) from exc
        finally:
            os.close(fd)

    def _materialize(self, provider: str, credential: str, root: Path) -> tuple[dict[str, str], dict[str, Any]]:
        env = {
            str(key): str(value)
            for key, value in self.base_environment.items()
            if str(key) not in _PROVIDER_SECRET_KEYS
        }
        home = root / "home"
        config = root / "config"
        cache = root / "cache"
        self._secure_directory(home)
        self._secure_directory(config)
        self._secure_directory(cache)
        env.update({
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
        })
        state: dict[str, Any] = {}
        if provider == "openai-codex":
            codex_home = root / "codex"
            self._secure_directory(codex_home)
            auth_path = codex_home / "auth.json"
            self._secure_write(auth_path, credential)
            env["CODEX_HOME"] = str(codex_home)
            state.update({
                "auth_path": auth_path,
                "initial_digest": hashlib.sha256(credential.encode()).hexdigest(),
            })
        elif provider == "anthropic-claude":
            claude_home = root / "claude"
            self._secure_directory(claude_home)
            env["CLAUDE_CONFIG_DIR"] = str(claude_home)
            env["CLAUDE_CODE_OAUTH_TOKEN"] = credential
        elif provider == "cursor":
            cursor_home = root / "cursor"
            self._secure_directory(cursor_home)
            env["CURSOR_CONFIG_DIR"] = str(cursor_home)
            env["CURSOR_API_KEY"] = credential
        return env, state

    @staticmethod
    def _preflight_result(provider: str, completed: Any) -> dict[str, Any]:
        if int(getattr(completed, "returncode", 1) or 0) != 0:
            return {"authenticated": False, "error_code": "provider_auth_preflight_failed"}
        stdout = str(getattr(completed, "stdout", "") or "")
        stderr = str(getattr(completed, "stderr", "") or "")
        if provider == "openai-codex":
            normalized = f"{stdout}\n{stderr}".strip().lower()
            authenticated = "logged in" in normalized and "not logged in" not in normalized
            return {
                "authenticated": authenticated,
                "auth_mode": "chatgpt_personal" if authenticated else "unknown",
                **({} if authenticated else {"error_code": "provider_auth_preflight_failed"}),
            }
        payload = _safe_json_object(stdout)
        if not payload:
            return {"authenticated": False, "error_code": "provider_auth_preflight_malformed"}
        authenticated = _truthy_field(
            payload, "authenticated", "isAuthenticated", "loggedIn", "logged_in")
        status_value = str(payload.get("status") or "").strip().lower()
        authenticated = authenticated or status_value in {"authenticated", "logged_in", "ready"}
        if provider == "anthropic-claude":
            method = str(payload.get("authMethod") or payload.get("auth_method") or "").lower()
            api_provider = str(payload.get("apiProvider") or payload.get("api_provider") or "").lower()
            disallowed = any(value in f"{method} {api_provider}" for value in (
                "api_key", "api key", "bedrock", "vertex"))
            authenticated = authenticated and not disallowed
            return {
                "authenticated": authenticated,
                "auth_mode": "oauth_personal" if authenticated else "unknown",
                **({} if authenticated else {"error_code": "provider_auth_preflight_failed"}),
            }
        return {
            "authenticated": authenticated,
            "auth_mode": "personal_api_key" if authenticated else "unknown",
            **({} if authenticated else {"error_code": "provider_auth_preflight_failed"}),
        }

    @staticmethod
    def _output_metadata(completed: Any) -> dict[str, Any]:
        """Return useful diagnostics without returning any provider output."""
        stdout = str(getattr(completed, "stdout", "") or "").encode()
        stderr = str(getattr(completed, "stderr", "") or "").encode()
        return {
            "exit_code": int(getattr(completed, "returncode", 1) or 0),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "provider_output_redacted": True,
        }

    def _preflight_once(
        self, provider: str, env: Mapping[str, str], cwd: str | None,
    ) -> dict[str, Any]:
        executable = self.cli_paths.get(provider)
        if not executable:
            candidates = _PROVIDER_CLI_CANDIDATES.get(provider) or (_PROVIDER_CLI[provider],)
            executable = next(
                (resolved for candidate in candidates
                 if (resolved := shutil.which(candidate, path=env.get("PATH")))),
                candidates[0],
            )
        command = [executable, *_PROVIDER_PREFLIGHT[provider]]
        try:
            completed = self.command_runner(
                command,
                env=dict(env),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.preflight_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "authenticated": False,
                "error_code": "provider_auth_preflight_unavailable",
                "failure_kind": type(exc).__name__,
                "provider_output_redacted": True,
                "provider": provider,
                "command": " ".join(_PROVIDER_PREFLIGHT[provider]),
            }
        result = self._preflight_result(provider, completed)
        result.update(self._output_metadata(completed))
        result["provider"] = provider
        result["command"] = " ".join(_PROVIDER_PREFLIGHT[provider])
        return result

    def _preflight(self, provider: str, env: Mapping[str, str], cwd: str | None) -> dict[str, Any]:
        """Run a bounded preflight retry loop and return only redacted evidence."""
        result: dict[str, Any] = {}
        for attempt in range(1, self.preflight_attempts + 1):
            result = self._preflight_once(provider, env, cwd)
            result["attempt_count"] = attempt
            if result.get("authenticated"):
                return result
            if attempt < self.preflight_attempts and self.preflight_retry_delay_seconds:
                self.sleep_fn(self.preflight_retry_delay_seconds)
        return result

    @staticmethod
    def _stop_process(process: Any) -> None:
        if process is None:
            return
        try:
            if hasattr(process, "poll") and process.poll() is not None:
                return
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass

    def run(
        self,
        binding: Mapping[str, Any],
        *,
        lease_id: str,
        principal: CredentialPrincipal | Mapping[str, Any],
        actor: str,
        command: Sequence[str],
        cwd: str | None = None,
        validate_runtime: bool = True,
    ) -> dict[str, Any]:
        """Run one command and return only redacted provider/account provenance."""
        try:
            provider = normalize_provider(str(binding.get("provider") or ""))
            credential_principal = (
                principal if isinstance(principal, CredentialPrincipal)
                else CredentialPrincipal.from_mapping(principal)
            )
        except CredentialPolicyError as exc:
            return {
                "schema": PROVIDER_RUNTIME_RECEIPT_SCHEMA,
                "allowed": False,
                "status": "denied",
                "error_code": exc.code,
            }
        lane_command = [str(item) for item in command]
        if not lane_command or any(not item or "\x00" in item for item in lane_command):
            return {
                "schema": PROVIDER_RUNTIME_RECEIPT_SCHEMA,
                "allowed": False,
                "status": "denied",
                "error_code": "provider_runtime_command_invalid",
            }

        state: dict[str, Any] = {"root": None, "process": None, "materialized": {}}
        receipt: dict[str, Any] = {
            "schema": PROVIDER_RUNTIME_RECEIPT_SCHEMA,
            "allowed": False,
            "status": "denied",
            "provider": provider,
            "customer_user_id": str(binding.get("user_id") or ""),
            "provider_account": _account_fingerprint(
                provider, str(binding.get("provider_account_id") or "")),
            "acquiring_principal": {
                "principal_id": credential_principal.principal_id,
                "principal_kind": credential_principal.principal_kind,
            },
            "runner_session_id": str(binding.get("runner_session_id") or ""),
            "work_session_id": str(binding.get("work_session_id") or ""),
            "lease_id": str(lease_id or ""),
            "residue_purged": False,
        }

        def purge() -> None:
            root = state.get("root")
            if root:
                shutil.rmtree(root, ignore_errors=True)
                state["root"] = None
            receipt["residue_purged"] = not bool(root and Path(root).exists())

        def starter(credential: str) -> dict[str, Any]:
            if credential and any(credential in item for item in lane_command):
                return {"started": False, "status": "secret_in_argv_denied"}
            root = self._runtime_root(provider)
            state["root"] = root
            env, materialized = self._materialize(provider, credential, root)
            state["materialized"] = materialized
            preflight = self._preflight(provider, env, cwd)
            state["preflight"] = preflight
            if not preflight.get("authenticated"):
                return {"started": False, "status": "auth_preflight_failed"}
            try:
                process = self.process_factory(
                    lane_command,
                    env=env,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            finally:
                for key in ("CLAUDE_CODE_OAUTH_TOKEN", "CURSOR_API_KEY"):
                    env.pop(key, None)
            state["process"] = process
            return {
                "started": True,
                "pid": int(getattr(process, "pid", 0) or 0),
                "runner_session_id": binding.get("runner_session_id"),
                "status": "running",
            }

        raised: BaseException | None = None
        try:
            launched = start_with_provider_credential(
                dict(binding),
                lease_id=lease_id,
                actor=actor,
                start_process=starter,
                principal=credential_principal,
                purge_runtime=purge,
                repository=self.repository,
                validate_runtime=validate_runtime,
            )
            receipt["auth_preflight"] = dict(state.get("preflight") or {})
            if not launched.get("allowed"):
                receipt["error_code"] = launched.get("error_code") or "provider_launch_denied"
            else:
                receipt.update({
                    "allowed": True,
                    "status": "running",
                    "pid": int(launched.get("pid") or 0),
                })
                process = state.get("process")
                try:
                    exit_code = int(process.wait())
                except BaseException as exc:  # cleanup must also cover interrupts/termination
                    self._stop_process(process)
                    raised = exc
                else:
                    receipt["exit_code"] = exit_code
                    receipt["status"] = "completed" if exit_code == 0 else "failed"
                    if provider == "openai-codex" and exit_code == 0:
                        materialized = state.get("materialized") or {}
                        capsule = self._read_secure_codex_capsule(materialized["auth_path"])
                        digest = hashlib.sha256(capsule.encode()).hexdigest()
                        if digest != materialized.get("initial_digest"):
                            receipt["codex_writeback"] = self.repository.writeback_active_codex_capsule(
                                lease_id,
                                credential=capsule,
                                actor=actor,
                                principal=credential_principal,
                            )
                        else:
                            receipt["codex_writeback"] = {"written_back": False, "reason": "unchanged"}
        except (CredentialVaultError, OSError, subprocess.SubprocessError):
            receipt["status"] = "failed"
            receipt["error_code"] = "provider_runtime_failed"
        finally:
            purge()
            try:
                released = self.repository.release_lease(
                    lease_id,
                    project=str(binding.get("project") or ""),
                    actor=actor,
                    reason="provider_runtime_exit",
                    principal=credential_principal,
                )
                receipt["lease_state"] = released.get("state")
            except CredentialVaultError:
                receipt["lease_state"] = "release_failed"
                if receipt.get("status") not in {"denied", "failed"}:
                    receipt["status"] = "failed"
                    receipt["error_code"] = "credential_lease_release_failed"
        if raised is not None:
            raise raised
        return receipt


__all__ = [
    "PROVIDER_RUNTIME_RECEIPT_SCHEMA",
    "ProviderRuntimeAuth",
]
