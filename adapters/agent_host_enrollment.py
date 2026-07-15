#!/usr/bin/env python3
"""Signed personal Agent Host install, update, rotate, revoke, and uninstall CLI.

The lifecycle is deliberately host-owned. Switchboard receives a narrow rotatable
host bearer plus redacted capability/account proof; provider credentials and the
Codex personal login remain on the user's machine.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform as platform_module
import plistlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


AGENT_HOST_VERSION = "0.2.0"
BUNDLE_SCHEMA = "switchboard.agent_host_bundle.v1"
LOCAL_STATE_SCHEMA = "switchboard.agent_host_local_state.v1"
IDENTITY_SCHEMA = "switchboard.agent_host_identity.v1"
SERVICE_LABEL = "com.6thelement.switchboard-agent-host"
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
_SECRET_MARKERS = (
    b"aht-",
    b"ahb-",
    b"ahr-",
    b"OPENAI_API_KEY=",
    b"CODEX_API_KEY=",
    b"CODEX_ACCESS_TOKEN=",
    b"ANTHROPIC_API_KEY=",
)
_METERED_PROVIDER_ENV = {
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
}


class EnrollmentError(RuntimeError):
    """Fail-closed lifecycle error with a stable operator-facing message."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(str(value or ""))
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise EnrollmentError(f"unsafe bundle path: {value!r}")
    return path


def _parse_version(value: str) -> tuple[int, int, int]:
    match = _SEMVER_RE.fullmatch(str(value or "").strip())
    if not match:
        raise EnrollmentError("bundle version must be semantic version x.y.z")
    return tuple(int(match.group(index)) for index in (1, 2, 3))


def _atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as target:
        json.dump(value, target, sort_keys=True, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentError(f"cannot read {path}") from exc
    if not isinstance(value, dict):
        raise EnrollmentError(f"{path} must contain a JSON object")
    return value


def generate_host_identity() -> tuple[str, str]:
    """Return PEM private key plus a non-secret SHA-256 public-key fingerprint."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_pem, "sha256:" + hashlib.sha256(public_raw).hexdigest()


def _write_built_bundle(output_dir: Path, manifest: dict[str, Any],
                        key: Ed25519PrivateKey) -> None:
    manifest_bytes = _canonical_json(manifest)
    (output_dir / "manifest.json").write_bytes(manifest_bytes + b"\n")
    (output_dir / "manifest.sig").write_text(
        base64.b64encode(key.sign(manifest_bytes)).decode("ascii") + "\n",
        encoding="ascii",
    )


def create_signed_bundle(source_root: Path, output_dir: Path, version: str,
                         private_key_path: Path) -> dict[str, Any]:
    """Public bundle builder; split from verification to keep installer read-only."""
    _parse_version(version)
    source_root = source_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise EnrollmentError(f"output directory already exists: {output_dir}")
    payload = output_dir / "payload"
    payload.mkdir(parents=True)
    candidates = sorted(
        path for path in (source_root / "adapters").rglob("*.py")
        if "__pycache__" not in path.parts
    )
    candidates.extend(sorted(
        path for path in (source_root / "src" / "switchboard").rglob("*.py")
        if "__pycache__" not in path.parts
    ))
    candidates.extend(sorted(
        path for path in (source_root / "db").rglob("*.py")
        if "__pycache__" not in path.parts
    ))
    candidates.extend(path for path in (
        source_root / "constants.py",
        source_root / "store.py",
        source_root / "scripts" / "switchboard_path.py",
    ) if path.is_file())
    candidates.extend(path for path in (
        source_root / "deploy" / "agent-host" / "launchd.plist.in",
        source_root / "deploy" / "agent-host" / "systemd.service.in",
    ) if path.is_file())
    if not candidates:
        raise EnrollmentError("bundle source contains no Agent Host payload")
    files: list[dict[str, Any]] = []
    for source in sorted(set(candidates)):
        relative = source.relative_to(source_root)
        target = payload / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        executable = source.read_bytes().startswith(b"#!")
        mode = 0o755 if executable else 0o644
        os.chmod(target, mode)
        files.append({"path": relative.as_posix(), "sha256": _sha256(target), "mode": mode})
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "version": version,
        "agent_host_version": version,
        "platforms": ["darwin", "linux"],
        "entrypoint": "adapters/agent_host_enrollment.py",
        "files": files,
    }
    key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise EnrollmentError("bundle signing key must be Ed25519")
    _write_built_bundle(output_dir, manifest, key)
    return manifest


def verify_bundle(bundle_dir: Path, public_key_path: Path) -> dict[str, Any]:
    """Verify signature, supported platform, safe paths, exact hashes, and modes."""
    bundle_dir = bundle_dir.resolve()
    manifest = _read_json(bundle_dir / "manifest.json")
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise EnrollmentError("unsupported Agent Host bundle schema")
    _parse_version(str(manifest.get("version") or ""))
    key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise EnrollmentError("bundle verification key must be Ed25519")
    try:
        signature = base64.b64decode((bundle_dir / "manifest.sig").read_text().strip(), validate=True)
        key.verify(signature, _canonical_json(manifest))
    except (OSError, ValueError, InvalidSignature) as exc:
        raise EnrollmentError("Agent Host bundle signature verification failed") from exc
    declared: set[str] = set()
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            raise EnrollmentError("bundle file record must be an object")
        relative = _safe_relative(str(item.get("path") or ""))
        name = relative.as_posix()
        if name in declared:
            raise EnrollmentError(f"duplicate bundle path: {name}")
        declared.add(name)
        path = bundle_dir / "payload" / Path(*relative.parts)
        if not path.is_file() or path.is_symlink():
            raise EnrollmentError(f"bundle payload is missing regular file: {name}")
        if _sha256(path) != item.get("sha256"):
            raise EnrollmentError(f"bundle hash mismatch: {name}")
        mode = int(item.get("mode") or 0)
        if mode not in {0o644, 0o755}:
            raise EnrollmentError(f"unsafe bundle mode: {name}")
    actual = {
        path.relative_to(bundle_dir / "payload").as_posix()
        for path in (bundle_dir / "payload").rglob("*") if path.is_file()
    }
    if actual != declared:
        raise EnrollmentError("bundle contains undeclared or missing payload files")
    return manifest


def request_json(method: str, url: str, body: dict[str, Any], token: str = "",
                 timeout: int = 30) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=_canonical_json(body), headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EnrollmentError(f"Switchboard request failed: {url}") from exc
    if not isinstance(result, dict):
        raise EnrollmentError("Switchboard returned a non-object response")
    if result.get("error") or result.get("error_code"):
        raise EnrollmentError(str(result.get("message") or result.get("error")))
    return result


def preflight_codex_local_auth(
        *, codex_executable: str = "",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    """Prove the native Codex CLI and local ChatGPT login without exporting it."""
    executable = str(codex_executable or shutil.which("codex") or "").strip()
    if not executable:
        raise EnrollmentError("native codex CLI is not installed or not on PATH")
    env = os.environ.copy()
    for key in _METERED_PROVIDER_ENV:
        env.pop(key, None)
    results: list[subprocess.CompletedProcess] = []
    for command in ([executable, "--version"], [executable, "login", "status"]):
        completed = runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
        if completed.returncode != 0:
            raise EnrollmentError(
                "native Codex CLI local-auth preflight failed; sign in on this host first")
        results.append(completed)
    version = ((results[0].stdout or results[0].stderr or "").strip().splitlines()
               or ["codex"])[0][:120]
    status_lines = [
        line.strip() for line in (
            (results[1].stdout or "") + "\n" + (results[1].stderr or "")
        ).splitlines() if line.strip()
    ]
    # Codex currently exposes no JSON flag for `login status`; fail closed on its
    # explicit mode line instead of treating any successful exit as ChatGPT auth.
    if status_lines != ["Logged in using ChatGPT"]:
        raise EnrollmentError(
            "native Codex CLI must be logged in explicitly using ChatGPT; "
            "API-key, unknown, and ambiguous login modes are not accepted")
    proof_material = f"{version}\nchatgpt_personal"
    return {
        "schema": "switchboard.codex_local_auth_preflight.v1",
        "native_cli": True,
        "cli_version": version,
        "authenticated": True,
        "auth_mode": "chatgpt_personal",
        "account_fingerprint": "acct-" + hashlib.sha256(
            proof_material.encode("utf-8", errors="replace")).hexdigest()[:16],
        "credential_values_redacted": True,
        "provider_credential_exported": False,
    }


def _install_release(bundle_dir: Path, manifest: dict[str, Any], prefix: Path) -> Path:
    version = str(manifest["version"])
    releases = prefix / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / version
    temporary = releases / f".{version}.{os.getpid()}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(bundle_dir / "payload", temporary, symlinks=False)
    for item in manifest["files"]:
        relative = _safe_relative(item["path"])
        os.chmod(temporary / Path(*relative.parts), int(item["mode"]))
    if final.exists():
        shutil.rmtree(temporary)
    else:
        os.replace(temporary, final)
    current = prefix / "current"
    new_link = prefix / f".current.{os.getpid()}.tmp"
    if new_link.exists() or new_link.is_symlink():
        new_link.unlink()
    new_link.symlink_to(final)
    os.replace(new_link, current)
    return final


def _default_paths(target_platform: str) -> dict[str, Path]:
    home = Path.home()
    if target_platform == "darwin":
        service = home / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        state_root = home / "Library" / "Application Support" / "SwitchboardAgentHost"
        log_root = home / "Library" / "Logs" / "SwitchboardAgentHost"
    else:
        service = home / ".config" / "systemd" / "user" / "switchboard-agent-host.service"
        state_root = home / ".local" / "state" / "switchboard-agent-host"
        log_root = state_root / "logs"
    return {
        "prefix": home / ".local" / "share" / "switchboard-agent-host",
        "config_root": home / ".config" / "switchboard-agent-host",
        "state_root": state_root,
        "log_root": log_root,
        "service_path": service,
    }


def render_service(target_platform: str, *, python: str, entrypoint: Path,
                   identity_path: Path, config_path: Path,
                   service_path: Path, log_root: Path,
                   writable_roots: Iterable[Path] = ()) -> None:
    service_path.parent.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)
    arguments = [python, str(entrypoint), "service-run", "--identity", str(identity_path),
                 "--config", str(config_path)]
    if target_platform == "darwin":
        payload = {
            "Label": SERVICE_LABEL,
            "ProgramArguments": arguments,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 5,
            "StandardOutPath": str(log_root / "agent-host.log"),
            "StandardErrorPath": str(log_root / "agent-host.err.log"),
        }
        temporary = service_path.with_suffix(".plist.tmp")
        with temporary.open("wb") as target:
            plistlib.dump(payload, target, sort_keys=True)
        os.chmod(temporary, 0o644)
        os.replace(temporary, service_path)
    elif target_platform == "linux":
        quoted = " ".join(_systemd_quote(value) for value in arguments)
        roots = sorted({
            str(identity_path.parent),
            str(config_path.parent),
            str(log_root),
            *(str(Path(root)) for root in writable_roots),
        })
        content = (
            "[Unit]\nDescription=Switchboard personal Agent Host\nAfter=network-online.target\n"
            "Wants=network-online.target\n\n[Service]\nType=simple\n"
            f"ExecStart={quoted}\nRestart=always\nRestartSec=5\nNoNewPrivileges=yes\n"
            "PrivateTmp=yes\nProtectSystem=strict\nProtectHome=read-only\n"
            f"ReadWritePaths={' '.join(_systemd_quote(root) for root in roots)}\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        temporary = service_path.with_suffix(".service.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, service_path)
    else:
        raise EnrollmentError("target platform must be darwin or linux")


def _systemd_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def control_service(target_platform: str, action: str, service_path: Path,
                    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    """Operate only the current user's launchd/systemd service."""
    if target_platform == "darwin":
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{SERVICE_LABEL}"
        if action == "start":
            commands = [["launchctl", "bootstrap", domain, str(service_path)]]
        elif action == "stop":
            commands = [["launchctl", "bootout", target]]
        elif action == "restart":
            commands = [["launchctl", "kickstart", "-k", target]]
        else:
            raise EnrollmentError(f"unsupported service action: {action}")
    elif target_platform == "linux":
        if action == "start":
            commands = [["systemctl", "--user", "daemon-reload"],
                        ["systemctl", "--user", "enable", "--now", service_path.name]]
        elif action == "stop":
            commands = [["systemctl", "--user", "disable", "--now", service_path.name]]
        elif action == "restart":
            commands = [["systemctl", "--user", "daemon-reload"],
                        ["systemctl", "--user", "restart", service_path.name]]
        else:
            raise EnrollmentError(f"unsupported service action: {action}")
    else:
        raise EnrollmentError("target platform must be darwin or linux")
    for command in commands:
        result = runner(command, capture_output=True, text=True, check=False, timeout=30)
        # Stopping an already-unloaded service is idempotent.
        if result.returncode and not (action == "stop" and result.returncode in {3, 5, 113}):
            raise EnrollmentError(
                f"service {action} failed: {(result.stderr or result.stdout).strip()}")


_FINALIZATION_STATUSES = {
    "enrollment_completed_pending_finalize",
    "install_finalization_retry_required",
}


def _finalize_install(
    *, state: dict[str, Any], state_path: Path, identity_path: Path,
    config_path: Path, target_platform: str, prefix: Path, service_path: Path,
    log_root: Path, state_root: Path, entrypoint: str, start_service: bool,
    service_runner: Callable[..., subprocess.CompletedProcess],
) -> dict[str, Any]:
    """Idempotently finish a journaled enrollment after server completion."""
    identity = dict(state.get("pending_identity") or {})
    config = dict(state.get("pending_config") or {})
    if not identity.get("host_id") or not identity.get("host_token"):
        raise EnrollmentError("pending enrollment finalization lacks host identity material")
    if not config.get("base_url") or not config.get("project"):
        raise EnrollmentError("pending enrollment finalization lacks endpoint configuration")
    try:
        _atomic_json(identity_path, identity, 0o600)
        state["finalization_step"] = "identity_written"
        _atomic_json(state_path, state, 0o600)
        _atomic_json(config_path, config, 0o600)
        state["finalization_step"] = "config_written"
        _atomic_json(state_path, state, 0o600)
        render_service(
            target_platform,
            python=sys.executable,
            entrypoint=prefix / "current" / entrypoint,
            identity_path=identity_path,
            config_path=config_path,
            service_path=service_path,
            log_root=log_root,
            writable_roots=(state_root,),
        )
        state["finalization_step"] = "service_rendered"
        _atomic_json(state_path, state, 0o600)
        if start_service:
            # A retry may follow an ambiguous service-manager response. Stop is
            # intentionally idempotent, then start establishes one known instance.
            if state.get("finalization_attempted_service_start"):
                control_service(
                    target_platform, "stop", service_path, runner=service_runner)
            state["finalization_attempted_service_start"] = True
            _atomic_json(state_path, state, 0o600)
            control_service(target_platform, "start", service_path, runner=service_runner)
            state["finalization_step"] = "service_started"
            _atomic_json(state_path, state, 0o600)
    except Exception:
        state["status"] = "install_finalization_retry_required"
        state["finalization_failed_at"] = time.time()
        _atomic_json(state_path, state, 0o600)
        raise
    host_id = identity["host_id"]
    completion_recovered = bool(state.get("completion_recovered"))
    state.pop("pending_identity", None)
    state.pop("pending_config", None)
    state.pop("finalization_attempted_service_start", None)
    state.update({
        "status": "installed",
        "installed_at": time.time(),
        "finalization_step": "complete",
    })
    _atomic_json(state_path, state, 0o600)
    return {
        "installed": True,
        "host_id": host_id,
        "version": state["version"],
        "state_path": str(state_path),
        "completion_recovered": completion_recovered,
    }


def install_host(*, bundle_dir: Path, public_key_path: Path, bootstrap_code: str,
                 base_url: str, project: str, owner_user_id: str,
                 target_platform: str, paths: dict[str, Path] | None = None,
                 allow_work: bool = True, lanes: Iterable[str] = (),
                 tenant_allowlist: Iterable[str] = (),
                 provider_allowlist: Iterable[str] = ("openai-codex",),
                 start_service: bool = True,
                 http: Callable[..., dict[str, Any]] = request_json,
                 service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 local_auth_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 codex_executable: str = "",
                 hostname: str = "") -> dict[str, Any]:
    """Prepare a durable local install, consume bootstrap once, then start."""
    manifest = verify_bundle(bundle_dir, public_key_path)
    bootstrap_code = str(bootstrap_code or "").strip()
    if not bootstrap_code:
        raise EnrollmentError("bootstrap_code is required")
    if target_platform not in manifest.get("platforms", []):
        raise EnrollmentError(f"bundle does not support {target_platform}")
    local_auth = preflight_codex_local_auth(
        codex_executable=codex_executable, runner=local_auth_runner)
    selected = dict(paths or _default_paths(target_platform))
    prefix = Path(selected["prefix"])
    config_root = Path(selected["config_root"])
    state_root = Path(selected["state_root"])
    service_path = Path(selected["service_path"])
    log_root = Path(selected["log_root"])
    release = _install_release(bundle_dir, manifest, prefix)
    identity_path = config_root / "identity.json"
    config_path = config_root / "config.json"
    state_path = state_root / "state.json"
    bootstrap_fingerprint = "sha256:" + hashlib.sha256(bootstrap_code.encode()).hexdigest()
    retrying = identity_path.exists() or state_path.exists()
    if retrying:
        if not state_path.is_file():
            raise EnrollmentError("incomplete existing Agent Host state; revoke or uninstall first")
        state = _read_json(state_path)
        same_install = (
            state.get("bootstrap_fingerprint") == bootstrap_fingerprint
            and state.get("project") == project
            and state.get("platform") == target_platform
            and state.get("base_url") == base_url.rstrip("/")
        )
        if state.get("status") in _FINALIZATION_STATUSES:
            if not same_install:
                raise EnrollmentError(
                    "existing Agent Host finalization does not match this enrollment")
            state.update({"version": manifest["version"], "release": str(release),
                          "finalization_resumed_at": time.time()})
            _atomic_json(state_path, state, 0o600)
            return _finalize_install(
                state=state, state_path=state_path, identity_path=identity_path,
                config_path=config_path, target_platform=target_platform, prefix=prefix,
                service_path=service_path, log_root=log_root, state_root=state_root,
                entrypoint=manifest["entrypoint"], start_service=start_service,
                service_runner=service_runner,
            )
        if not identity_path.is_file():
            raise EnrollmentError("incomplete existing Agent Host state; revoke or uninstall first")
        pending_identity = _read_json(identity_path)
        retryable = (
            pending_identity.get("status") == "pending_enrollment"
            and state.get("status") in {
                "prepared_for_enrollment", "enrollment_retry_required",
                "enrollment_response_incomplete",
            }
            and state.get("bootstrap_fingerprint") == bootstrap_fingerprint
            and state.get("project") == project
            and state.get("platform") == target_platform
            and state.get("base_url") == base_url.rstrip("/")
        )
        if not retryable:
            raise EnrollmentError(
                "existing Agent Host identity does not match this pending enrollment")
        private_key_pem = str(pending_identity.get("private_key_pem") or "")
        fingerprint = str(pending_identity.get("public_key_fingerprint") or "")
        completion_recovery_secret = str(
            pending_identity.get("completion_recovery_secret") or "")
        if (not private_key_pem or not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint)
                or not re.fullmatch(r"ahr-[A-Za-z0-9_-]{32,}", completion_recovery_secret)):
            raise EnrollmentError("pending Agent Host enrollment material is incomplete")
        state.update({
            "status": "prepared_for_enrollment",
            "version": manifest["version"],
            "release": str(release),
            "retry_started_at": time.time(),
        })
    else:
        private_key_pem, fingerprint = generate_host_identity()
        completion_recovery_secret = "ahr-" + secrets.token_urlsafe(32)
        state = {
            "schema": LOCAL_STATE_SCHEMA,
            "status": "prepared_for_enrollment",
            "version": manifest["version"],
            "platform": target_platform,
            "project": project,
            "bootstrap_fingerprint": bootstrap_fingerprint,
            "prefix": str(prefix),
            "release": str(release),
            "service_path": str(service_path),
            "identity_path": str(identity_path),
            "config_path": str(config_path),
            "base_url": base_url.rstrip("/"),
            "owner_user_id": owner_user_id,
            "allow_work": bool(allow_work),
            "lanes": sorted(set(lanes)),
            "tenant_allowlist": sorted(set(tenant_allowlist)),
            "provider_allowlist": sorted(set(provider_allowlist)),
            "prepared_at": time.time(),
        }
        # Prove the release and recovery-capable secret-storage path are durable
        # before the one-time bootstrap can be consumed. The service is not
        # rendered or started yet.
        _atomic_json(identity_path, {
            "schema": IDENTITY_SCHEMA,
            "status": "pending_enrollment",
            "public_key_fingerprint": fingerprint,
            "private_key_pem": private_key_pem,
            "completion_recovery_secret": completion_recovery_secret,
        }, 0o600)
    _atomic_json(state_path, state, 0o600)
    try:
        completed = http(
            "POST",
            base_url.rstrip("/") + "/ixp/v1/agent-host-enrollments/complete",
            {
                "schema": "switchboard.agent.complete_host_enrollment_command.v1",
                "project": project,
                "bootstrap_code": bootstrap_code,
                "hostname": hostname or platform_module.node(),
                "platform": target_platform,
                "public_key_fingerprint": fingerprint,
                "completion_recovery_secret": completion_recovery_secret,
                "agent_host_version": manifest["version"],
            },
        )
    except Exception:
        state["status"] = "enrollment_retry_required"
        _atomic_json(state_path, state, 0o600)
        raise
    enrollment = completed.get("enrollment") or {}
    host_token = str(completed.get("host_token") or "")
    if not enrollment.get("host_id") or not host_token:
        state["status"] = "enrollment_response_incomplete"
        _atomic_json(state_path, state, 0o600)
        raise EnrollmentError("enrollment completion omitted host identity material")
    identity = {
        "schema": IDENTITY_SCHEMA,
        "host_id": enrollment["host_id"],
        "enrollment_id": enrollment.get("enrollment_id"),
        "principal_id": enrollment.get("principal_id"),
        "identity_generation": enrollment.get("identity_generation", 1),
        "public_key_fingerprint": fingerprint,
        "private_key_pem": private_key_pem,
        "host_token": host_token,
    }
    config = {
        "base_url": base_url.rstrip("/"),
        "project": project,
        "runtime": "codex",
        "work_module": "adapters.codex_local_worker:run",
        "allow_work": bool(allow_work),
        "allow_global_claim": False,
        "lanes": sorted(set(lanes)),
        "owner_user_id": owner_user_id,
        "tenant_allowlist": sorted(set(tenant_allowlist)),
        "project_allowlist": enrollment.get("project_allowlist") or [project],
        "provider_allowlist": sorted(set(provider_allowlist)),
        "local_auth_account_proof": local_auth["account_fingerprint"],
        "repo_root": str(prefix / "current"),
        "runner_dir": str(state_root / "runner"),
        "runtime_root": str(state_root / "provider-runtimes"),
        "agent_host_version": manifest["version"],
    }
    # Journal the only returned bearer and all validated endpoint data before the
    # first post-completion write. Any later boundary can resume or revoke locally.
    state.update({
        "status": "enrollment_completed_pending_finalize",
        "completion_recovered": bool(completed.get("completion_recovered")),
        "pending_identity": identity,
        "pending_config": config,
        "finalization_step": "journaled",
        "enrollment_completed_at": time.time(),
    })
    _atomic_json(state_path, state, 0o600)
    return _finalize_install(
        state=state, state_path=state_path, identity_path=identity_path,
        config_path=config_path, target_platform=target_platform, prefix=prefix,
        service_path=service_path, log_root=log_root, state_root=state_root,
        entrypoint=manifest["entrypoint"], start_service=start_service,
        service_runner=service_runner,
    )


def update_host(*, bundle_dir: Path, public_key_path: Path, state_path: Path,
                restart_service: bool = True,
                service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    manifest = verify_bundle(bundle_dir, public_key_path)
    state = _read_json(state_path)
    if _parse_version(manifest["version"]) <= _parse_version(state.get("version") or ""):
        raise EnrollmentError("update bundle must be newer than the installed version")
    prefix = Path(state["prefix"])
    current = prefix / "current"
    previous = current.resolve() if current.exists() else None
    release = _install_release(bundle_dir, manifest, prefix)
    try:
        if restart_service:
            control_service(
                state["platform"], "restart", Path(state["service_path"]), runner=service_runner)
    except Exception:
        if previous:
            rollback = prefix / f".current.rollback.{os.getpid()}"
            rollback.symlink_to(previous)
            os.replace(rollback, current)
        raise
    state.update({"version": manifest["version"], "release": str(release),
                  "status": "installed", "updated_at": time.time()})
    _atomic_json(state_path, state, 0o600)
    return {"updated": True, "version": manifest["version"]}


def rotate_identity(*, identity_path: Path, config_path: Path,
                    http: Callable[..., dict[str, Any]] = request_json) -> dict[str, Any]:
    identity = _read_json(identity_path)
    config = _read_json(config_path)
    private_key_pem, fingerprint = generate_host_identity()
    result = http(
        "POST",
        config["base_url"] + "/ixp/v1/agent-host-enrollments/rotate",
        {"project": config["project"], "host_id": identity["host_id"],
         "public_key_fingerprint": fingerprint},
        identity["host_token"],
    )
    enrollment = result.get("enrollment") or {}
    new_token = str(result.get("host_token") or "")
    if not result.get("rotated") or not new_token:
        raise EnrollmentError("identity rotation omitted replacement bearer")
    identity.update({
        "host_token": new_token,
        "private_key_pem": private_key_pem,
        "public_key_fingerprint": fingerprint,
        "identity_generation": enrollment.get("identity_generation"),
        "rotated_at": time.time(),
    })
    _atomic_json(identity_path, identity, 0o600)
    return {"rotated": True, "identity_generation": identity["identity_generation"]}


def revoke_host(*, identity_path: Path, config_path: Path, state_path: Path,
                final_status: str = "revoked", stop_service: bool = True,
                http: Callable[..., dict[str, Any]] = request_json,
                service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    state = _read_json(state_path)
    identity = _read_json(identity_path) if identity_path.is_file() else {}
    config = _read_json(config_path) if config_path.is_file() else {}
    if not identity.get("host_token"):
        identity = dict(state.get("pending_identity") or {})
    if not config.get("base_url"):
        config = dict(state.get("pending_config") or {})
    if not identity.get("host_id") or not identity.get("host_token"):
        raise EnrollmentError("no completed host identity is available to revoke")
    if not config.get("base_url") or not config.get("project"):
        raise EnrollmentError("no enrollment endpoint is available to revoke the host")
    if stop_service:
        control_service(
            state["platform"], "stop", Path(state["service_path"]), runner=service_runner)
    try:
        result = http(
            "POST",
            config["base_url"] + "/ixp/v1/agent-host-enrollments/revoke",
            {"project": config["project"], "host_id": identity["host_id"],
             "reason": "local_host_revoke", "final_status": final_status},
            identity["host_token"],
        )
    except Exception:
        state.update({"status": "revocation_pending", "revocation_pending_at": time.time()})
        _atomic_json(state_path, state, 0o600)
        raise
    if not result.get("revoked"):
        raise EnrollmentError("Switchboard did not confirm host revocation")
    identity_path.unlink(missing_ok=True)
    runtime_root = Path(config.get("runtime_root") or "")
    if runtime_root.is_dir():
        shutil.rmtree(runtime_root)
    state.update({"status": final_status, "revoked_at": time.time(),
                  "post_revoke_denial": True})
    state.pop("pending_identity", None)
    state.pop("pending_config", None)
    _atomic_json(state_path, state, 0o600)
    return {"revoked": True, "status": final_status}


def uninstall_host(*, identity_path: Path, config_path: Path, state_path: Path,
                   http: Callable[..., dict[str, Any]] = request_json,
                   service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    state = _read_json(state_path)
    revoked = revoke_host(
        identity_path=identity_path,
        config_path=config_path,
        state_path=state_path,
        final_status="uninstalled",
        http=http,
        service_runner=service_runner,
    )
    Path(state["service_path"]).unlink(missing_ok=True)
    prefix = Path(state["prefix"])
    config_root = config_path.parent
    state_root = state_path.parent
    shutil.rmtree(prefix, ignore_errors=True)
    shutil.rmtree(config_root, ignore_errors=True)
    shutil.rmtree(state_root, ignore_errors=True)
    return {"uninstalled": True, **revoked}


def residue_scan(roots: Iterable[Path]) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    scanned = 0
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        paths = [root] if root.is_file() else list(root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.is_symlink():
                continue
            scanned += 1
            try:
                data = path.read_bytes()
            except OSError:
                continue
            markers = [marker.decode("ascii").rstrip("=") for marker in _SECRET_MARKERS if marker in data]
            if markers:
                hits.append({"path": str(path), "markers": markers})
    return {"schema": "switchboard.agent_host_residue_scan.v1", "scanned_files": scanned,
            "residue_count": len(hits), "residue_free": not hits, "hits": hits}


def service_run(identity_path: Path, config_path: Path) -> None:
    """Load narrow host identity locally and exec the Agent Host daemon."""
    identity = _read_json(identity_path)
    config = _read_json(config_path)
    mode = stat.S_IMODE(identity_path.stat().st_mode)
    if mode & 0o077:
        raise EnrollmentError("identity file permissions must be 0600")
    env = os.environ.copy()
    # A personal Codex login belongs to the host. Never let an inherited metered
    # provider credential cross into the supervised runtime by accident.
    for key in _METERED_PROVIDER_ENV:
        env.pop(key, None)
    values = {
        "PM_BASE": config["base_url"],
        "PM_PROJECT": config["project"],
        "PM_MCP_TOKEN": identity["host_token"],
        "PM_HOST_ID": identity["host_id"],
        "PM_HOST_ENROLLMENT_ID": identity.get("enrollment_id") or "",
        "PM_HOST_IDENTITY_GENERATION": identity.get("identity_generation") or 1,
        "PM_HOST_PUBLIC_KEY_FINGERPRINT": identity.get("public_key_fingerprint") or "",
        "PM_RUNTIME": config.get("runtime") or "codex",
        "PM_AGENT_WORK_MODULE": config.get("work_module") or "adapters.codex_local_worker:run",
        "PM_AGENT_HOST_ALLOW_WORK": "1" if config.get("allow_work") else "0",
        "PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM": "0",
        "PM_HOST_LANES": ",".join(config.get("lanes") or []),
        "PM_HOST_OWNER_USER_ID": config.get("owner_user_id") or "",
        "PM_HOST_TENANTS": ",".join(config.get("tenant_allowlist") or []),
        "PM_HOST_PROJECTS": ",".join(config.get("project_allowlist") or [config["project"]]),
        "PM_HOST_PROVIDERS": ",".join(config.get("provider_allowlist") or []),
        "PM_HOST_LOCAL_AUTH_AVAILABLE": "1",
        "PM_HOST_LOCAL_AUTH_MODE": "chatgpt_personal",
        "PM_HOST_LOCAL_AUTH_ACCOUNT_PROOF": config.get("local_auth_account_proof") or "",
        "PM_REPO_ROOT": config["repo_root"],
        "PM_RUNNER_DIR": config["runner_dir"],
        "PM_PROVIDER_RUNTIME_ROOT": config["runtime_root"],
        "PM_AGENT_HOST_VERSION": config.get("agent_host_version") or AGENT_HOST_VERSION,
    }
    env.update({key: str(value) for key, value in values.items()})
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(config["repo_root"]) / "src"),
        str(Path(config["repo_root"])),
        env.get("PYTHONPATH", ""),
    )))
    daemon = Path(config["repo_root"]) / "adapters" / "agent_host.py"
    os.execve(sys.executable, [sys.executable, str(daemon), "--interval", "10"], env)


def _platform(value: str = "") -> str:
    result = (value or platform_module.system()).strip().lower()
    if result in {"macos", "mac", "darwin"}:
        return "darwin"
    if result == "linux":
        return "linux"
    raise EnrollmentError("Agent Host enrollment supports macOS and Linux")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Switchboard personal Agent Host lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-bundle")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--signing-key", type=Path, required=True)
    verify = sub.add_parser("verify-bundle")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--public-key", type=Path, required=True)
    install = sub.add_parser("install")
    install.add_argument("--bundle", type=Path, required=True)
    install.add_argument("--public-key", type=Path, required=True)
    install.add_argument("--bootstrap-code-file", type=Path, required=True)
    install.add_argument("--base-url", default="https://plan.taikunai.com")
    install.add_argument("--project", default="switchboard")
    install.add_argument("--owner-user-id", required=True)
    install.add_argument("--platform", default="")
    install.add_argument("--lanes", default="")
    install.add_argument("--no-start", action="store_true")
    update = sub.add_parser("update")
    update.add_argument("--bundle", type=Path, required=True)
    update.add_argument("--public-key", type=Path, required=True)
    update.add_argument("--state", type=Path, required=True)
    update.add_argument("--no-restart", action="store_true")
    rotate = sub.add_parser("rotate")
    rotate.add_argument("--identity", type=Path, required=True)
    rotate.add_argument("--config", type=Path, required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--identity", type=Path, required=True)
    revoke.add_argument("--config", type=Path, required=True)
    revoke.add_argument("--state", type=Path, required=True)
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--identity", type=Path, required=True)
    uninstall.add_argument("--config", type=Path, required=True)
    uninstall.add_argument("--state", type=Path, required=True)
    run = sub.add_parser("service-run")
    run.add_argument("--identity", type=Path, required=True)
    run.add_argument("--config", type=Path, required=True)
    scan = sub.add_parser("residue-scan")
    scan.add_argument("roots", type=Path, nargs="+")
    sub.add_parser("preflight")
    args = parser.parse_args(argv)
    try:
        if args.command == "build-bundle":
            result = create_signed_bundle(
                args.source_root, args.output, args.version, args.signing_key)
        elif args.command == "verify-bundle":
            result = verify_bundle(args.bundle, args.public_key)
        elif args.command == "install":
            result = install_host(
                bundle_dir=args.bundle,
                public_key_path=args.public_key,
                bootstrap_code=args.bootstrap_code_file.read_text(encoding="utf-8").strip(),
                base_url=args.base_url,
                project=args.project,
                owner_user_id=args.owner_user_id,
                target_platform=_platform(args.platform),
                lanes=[item.strip() for item in args.lanes.split(",") if item.strip()],
                start_service=not args.no_start,
            )
        elif args.command == "update":
            result = update_host(
                bundle_dir=args.bundle,
                public_key_path=args.public_key,
                state_path=args.state,
                restart_service=not args.no_restart,
            )
        elif args.command == "rotate":
            result = rotate_identity(identity_path=args.identity, config_path=args.config)
        elif args.command == "revoke":
            result = revoke_host(
                identity_path=args.identity, config_path=args.config, state_path=args.state)
        elif args.command == "uninstall":
            result = uninstall_host(
                identity_path=args.identity, config_path=args.config, state_path=args.state)
        elif args.command == "service-run":
            service_run(args.identity, args.config)
            return 0
        elif args.command == "preflight":
            result = preflight_codex_local_auth()
        else:
            result = residue_scan(args.roots)
        print(json.dumps(result, sort_keys=True))
        return 0
    except EnrollmentError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
