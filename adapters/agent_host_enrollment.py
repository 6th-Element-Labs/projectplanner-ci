#!/usr/bin/env python3
"""Signed personal Agent Host install, update, rotate, revoke, and uninstall CLI.

The lifecycle is deliberately host-owned. Switchboard receives a narrow rotatable
host bearer plus redacted capability/account proof; provider credentials and the
Codex personal login remain on the user's machine.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import math
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
import tempfile
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
# BUG-99: deterministic runner-session PATH on macOS — Homebrew (arm64 + intel)
# ahead of the system defaults so gh/codex resolve by name inside sessions.
SERVICE_PATH_DARWIN = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Shared with agent_host.py's _declared_account_affinities() reader — a single
# source so the filename/key can never silently drift between writer and reader
# (a drift would fail silently: the reader's isinstance guard just returns []).
ACCOUNT_AFFINITIES_FILENAME = "account_affinities.json"
ACCOUNT_AFFINITY_IDS_KEY = "account_affinity_ids"
_SEMVER_IDENTIFIER = r"[0-9A-Za-z-]+"
_SEMVER_RE = re.compile(
    rf"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*))?"
    rf"(?:\+({_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*))?$"
)
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
_COORDINATION_CREDENTIAL_ENV = {
    "PM_MCP_TOKEN",
    "SWITCHBOARD_TOKEN",
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


def _parse_version(
        value: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    """Return a comparison key implementing SemVer 2.0 precedence exactly."""
    match = _SEMVER_RE.fullmatch(str(value or "").strip())
    if not match:
        raise EnrollmentError("bundle version must be a valid semantic version")
    prerelease = str(match.group(4) or "")
    identifiers: list[tuple[int, int | str]] = []
    for identifier in prerelease.split(".") if prerelease else ():
        if identifier.isdigit():
            if len(identifier) > 1 and identifier.startswith("0"):
                raise EnrollmentError(
                    "numeric semantic-version prerelease identifiers cannot have leading zeros")
            identifiers.append((0, int(identifier)))
        else:
            identifiers.append((1, identifier))
    # Stable releases sort after every prerelease. Build metadata is deliberately
    # absent from the key because SemVer excludes it from precedence.
    return (
        int(match.group(1)), int(match.group(2)), int(match.group(3)),
        0 if prerelease else 1,
        tuple(identifiers),
    )


def _cleanup_stale_atomic_temps(path: Path, *, now: float | None = None) -> None:
    """Remove only old, same-user regular files created by our atomic writer."""
    cutoff = (time.time() if now is None else now) - 3600
    prefix = f".{path.name}."
    for candidate in path.parent.glob(f"{prefix}*.tmp"):
        try:
            metadata = candidate.lstat()
            if (not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_mtime > cutoff):
                continue
            candidate.unlink()
        except FileNotFoundError:
            continue


def _atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    _cleanup_stale_atomic_temps(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            descriptor = -1
            json.dump(value, target, sort_keys=True, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


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
    payload_root = bundle_dir / "payload"
    if payload_root.is_symlink() or not payload_root.is_dir():
        raise EnrollmentError("bundle payload must be a regular directory")
    payload_entries = list(payload_root.rglob("*"))
    if any(path.is_symlink() for path in payload_entries):
        raise EnrollmentError("bundle payload may not contain symlinks")
    if any(not path.is_file() and not path.is_dir() for path in payload_entries):
        raise EnrollmentError("bundle payload contains a non-regular entry")
    declared: set[str] = set()
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            raise EnrollmentError("bundle file record must be an object")
        relative = _safe_relative(str(item.get("path") or ""))
        name = relative.as_posix()
        if name in declared:
            raise EnrollmentError(f"duplicate bundle path: {name}")
        declared.add(name)
        path = payload_root / Path(*relative.parts)
        if not path.is_file() or path.is_symlink():
            raise EnrollmentError(f"bundle payload is missing regular file: {name}")
        if _sha256(path) != item.get("sha256"):
            raise EnrollmentError(f"bundle hash mismatch: {name}")
        mode = int(item.get("mode") or 0)
        if mode not in {0o644, 0o755}:
            raise EnrollmentError(f"unsafe bundle mode: {name}")
    actual = {
        path.relative_to(payload_root).as_posix()
        for path in payload_entries if path.is_file()
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
        *, codex_executable: str = "", codex_home: str | Path = "",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, Any]:
    """Prove the native Codex CLI and local ChatGPT login without exporting it."""
    requested = str(codex_executable or "codex").strip()
    resolved = shutil.which(requested)
    if not resolved:
        raise EnrollmentError("native codex CLI is not installed or not on PATH")
    executable = str(Path(resolved).resolve())
    env = os.environ.copy()
    for key in _METERED_PROVIDER_ENV | _COORDINATION_CREDENTIAL_ENV:
        env.pop(key, None)
    if str(codex_home or "").strip():
        env["CODEX_HOME"] = str(Path(codex_home).expanduser().resolve())
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
        "codex_executable": executable,
        "account_fingerprint": "acct-" + hashlib.sha256(
            proof_material.encode("utf-8", errors="replace")).hexdigest()[:16],
        "credential_values_redacted": True,
        "provider_credential_exported": False,
    }


def prepare_dedicated_codex_home(
        target_root: Path, *, source_root: Path | None = None) -> Path:
    """Seed an Agent-Host-owned Codex auth root separate from the user's live home.

    The native worker needs durable refresh writes, while repository-controlled post-run
    tests must never inherit or read the same credential directory. Copy only the Codex
    auth object into a protected root. An existing target is preserved so an enrollment
    retry cannot overwrite a refreshed token with stale source material.
    """
    target = target_root.expanduser()
    if target.exists() and target.is_symlink():
        raise EnrollmentError("dedicated Codex home cannot be a symlink")
    source = (source_root or Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))).expanduser()
    if source.exists() and source.is_symlink():
        raise EnrollmentError("source Codex home cannot be a symlink")
    if source.resolve() == target.resolve():
        raise EnrollmentError("Agent Host requires a Codex home separate from the user login")
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    target_auth = target / "auth.json"
    if target_auth.exists():
        if not target_auth.is_file() or target_auth.is_symlink():
            raise EnrollmentError("dedicated Codex auth path must be a regular file")
        os.chmod(target_auth, 0o600)
        return target.resolve()
    source_auth = source / "auth.json"
    if (not source_auth.is_file() or source_auth.is_symlink()
            or source_auth.stat().st_uid != os.getuid()):
        raise EnrollmentError(
            "native Codex file auth is required; sign in with ChatGPT before enrollment")
    _atomic_json(target_auth, _read_json(source_auth), 0o600)
    return target.resolve()


def _install_release(bundle_dir: Path, manifest: dict[str, Any], prefix: Path) -> Path:
    version = str(manifest["version"])
    releases = prefix / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / version
    temporary = releases / f".{version}.{os.getpid()}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    # Preserve any link introduced after verification so it is rejected below;
    # never dereference unsigned content into an installed release.
    shutil.copytree(bundle_dir / "payload", temporary, symlinks=True)
    temporary_entries = list(temporary.rglob("*"))
    if (any(path.is_symlink() for path in temporary_entries)
            or any(not path.is_file() and not path.is_dir()
                   for path in temporary_entries)):
        shutil.rmtree(temporary)
        raise EnrollmentError("copied bundle payload contains an unsafe entry")
    copied_files = {
        path.relative_to(temporary).as_posix()
        for path in temporary_entries if path.is_file()
    }
    declared_files = {str(item["path"]) for item in manifest["files"]}
    if copied_files != declared_files:
        shutil.rmtree(temporary)
        raise EnrollmentError("copied bundle payload does not match its signed manifest")
    for item in manifest["files"]:
        relative = _safe_relative(item["path"])
        copied = temporary / Path(*relative.parts)
        if _sha256(copied) != item["sha256"]:
            shutil.rmtree(temporary)
            raise EnrollmentError("copied bundle payload hash mismatch")
        os.chmod(copied, int(item["mode"]))
    if final.exists():
        declared = {str(item["path"]): item for item in manifest["files"]}
        actual = {
            path.relative_to(final).as_posix()
            for path in final.rglob("*") if path.is_file() or path.is_symlink()
        }
        exact = actual == set(declared)
        for name, item in declared.items():
            path = final / Path(*_safe_relative(name).parts)
            exact = bool(
                exact and path.is_file() and not path.is_symlink()
                and _sha256(path) == item["sha256"]
                and stat.S_IMODE(path.stat().st_mode) == int(item["mode"])
            )
        if not exact:
            shutil.rmtree(temporary)
            raise EnrollmentError(
                "existing release does not match the newly verified signed bundle")
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
        "workspace_root": state_root / "workspaces",
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
            # BUG-99: launchd's default PATH (/usr/bin:/bin:/usr/sbin:/sbin)
            # omits Homebrew, so runner sessions could not resolve gh (or even
            # codex) by name. Task completion became nondeterministic: one
            # session hand-rolled a urllib call against the GitHub API to open
            # its PR while another honestly blocked on "gh is not installed".
            # The service environment must make the finishing step boring.
            "EnvironmentVariables": {"PATH": SERVICE_PATH_DARWIN},
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
            # kickstart restarts the process with launchd's already-loaded job
            # definition.  It does not reload a changed plist, so updates to
            # EnvironmentVariables (notably the Homebrew PATH used by gh/codex)
            # never reach the daemon or its children.  Reload the definition.
            commands = [
                ["launchctl", "bootout", target],
                ["launchctl", "bootstrap", domain, str(service_path)],
            ]
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
        # Unloading an already-unloaded service is idempotent, including the
        # first half of a restart/reload.
        unloading = len(command) > 1 and command[1] == "bootout"
        if result.returncode and not (unloading and result.returncode in {3, 5, 113}):
            raise EnrollmentError(
                f"service {action} failed: {(result.stderr or result.stdout).strip()}")


_FINALIZATION_STATUSES = {
    "enrollment_completed_pending_finalize",
    "install_finalization_retry_required",
    "installed_finalization_ack_pending",
}


def _paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either normalized path contains the other."""
    left_value = str(Path(left).expanduser().resolve())
    right_value = str(Path(right).expanduser().resolve())
    try:
        common = os.path.commonpath((left_value, right_value))
    except ValueError:
        return False
    return common in {left_value, right_value}


def _proper_child(path: Path, parent: Path) -> bool:
    path_value = Path(path).expanduser().resolve()
    parent_value = Path(parent).expanduser().resolve()
    try:
        path_value.relative_to(parent_value)
    except ValueError:
        return False
    return path_value != parent_value


def _lexists(path: Path) -> bool:
    """Return true for every directory entry, including a dangling symlink."""
    return os.path.lexists(Path(path).expanduser())


def _validated_source_repo_root(value: Path | str) -> Path:
    """Return an exact Git work source without trusting the signed runtime tree."""
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise EnrollmentError("source_repo_root must be an absolute non-symlink path")
    resolved = raw.resolve()
    if raw.absolute() != resolved or not resolved.is_dir():
        raise EnrollmentError("source_repo_root must resolve directly to a directory")

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(resolved), *arguments],
            capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise EnrollmentError("source_repo_root must be a usable Git checkout")
        return (result.stdout or "").strip()

    top_level = Path(git("rev-parse", "--show-toplevel")).resolve()
    if top_level != resolved:
        raise EnrollmentError("source_repo_root must name the Git checkout root")
    if not git("remote", "get-url", "origin"):
        raise EnrollmentError("source_repo_root must have a canonical origin remote")
    git("rev-parse", "HEAD")
    return resolved


def _provision_host_source_mirror(source_repo: Path, state_root: Path) -> Path:
    """Create/update the Agent Host's private clean source checkout.

    ``source_repo`` is used only to discover the canonical origin URL.  Work
    Sessions must never be based on that operator-owned checkout: an unrelated
    untracked file there must not take the whole host out of service.
    """
    origin = subprocess.run(
        ["git", "-C", str(source_repo), "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=30, check=False)
    origin_url = (origin.stdout or "").strip()
    if origin.returncode != 0 or not origin_url:
        raise EnrollmentError("source_repo_root must have a canonical origin remote")
    repo_name = Path(origin_url.removesuffix(".git")).name or "canonical"
    mirror_root = state_root / "source"
    mirror = mirror_root / repo_name
    mirror_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(mirror_root, 0o700)
    if not mirror.exists():
        cloned = subprocess.run(
            ["git", "clone", "--no-local", origin_url, str(mirror)],
            capture_output=True, text=True, timeout=600, check=False)
        if cloned.returncode != 0:
            raise EnrollmentError(
                "host source mirror clone failed: "
                + (cloned.stderr or "unknown git error").strip()[-500:])
    validated = _validated_source_repo_root(mirror)
    mirror_origin = subprocess.run(
        ["git", "-C", str(validated), "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=30, check=False)
    if (mirror_origin.stdout or "").strip() != origin_url:
        raise EnrollmentError("host source mirror origin does not match canonical origin")
    fetched = subprocess.run(
        ["git", "-C", str(validated), "fetch", "--prune", "origin"],
        capture_output=True, text=True, timeout=600, check=False)
    if fetched.returncode != 0:
        raise EnrollmentError(
            "host source mirror fetch failed: "
            + (fetched.stderr or "unknown git error").strip()[-500:])
    head_ref = subprocess.run(
        ["git", "-C", str(validated), "symbolic-ref", "--short",
         "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, timeout=30, check=False)
    remote_tracking_ref = (head_ref.stdout or "").strip()
    if head_ref.returncode != 0 or not remote_tracking_ref.startswith("origin/"):
        raise EnrollmentError("host source mirror cannot resolve origin default branch")
    branch = remote_tracking_ref.split("/", 1)[1]
    local_head = subprocess.run(
        ["git", "-C", str(validated), "rev-parse", remote_tracking_ref],
        capture_output=True, text=True, timeout=30, check=False)
    remote_head = subprocess.run(
        ["git", "-C", str(validated), "ls-remote", "--exit-code", "origin",
         f"refs/heads/{branch}"],
        capture_output=True, text=True, timeout=60, check=False)
    local_sha = (local_head.stdout or "").strip()
    remote_sha = ((remote_head.stdout or "").strip().split(None, 1) or [""])[0]
    if (local_head.returncode != 0 or remote_head.returncode != 0
            or not remote_sha or local_sha != remote_sha):
        raise EnrollmentError(
            f"host source mirror is stale: {remote_tracking_ref}={local_sha or '<missing>'}, "
            f"origin refs/heads/{branch}={remote_sha or '<missing>'}")
    dirty = subprocess.run(
        ["git", "-C", str(validated), "status", "--porcelain"],
        capture_output=True, text=True, timeout=30, check=False)
    dirty_paths = [line[3:] if len(line) > 3 else line
                   for line in (dirty.stdout or "").splitlines() if line.strip()]
    if dirty.returncode != 0 or dirty_paths:
        names = ", ".join(dirty_paths) or "<git status failed>"
        raise EnrollmentError(f"host source mirror is dirty: {names}")
    return validated


def _private_key_matches_fingerprint(private_key_pem: str, fingerprint: str) -> bool:
    """Cryptographically bind retry key material to its recorded public fingerprint."""
    try:
        private_key = serialization.load_pem_private_key(
            str(private_key_pem or "").encode("ascii"), password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            return False
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    actual = "sha256:" + hashlib.sha256(public_raw).hexdigest()
    return secrets.compare_digest(actual, str(fingerprint or ""))


def _validate_install_layout(
        *, prefix: Path, config_root: Path, state_root: Path,
        workspace_root: Path, codex_home: Path, log_root: Path,
        service_path: Path, runner_root: Path | None = None,
        runtime_root: Path | None = None,
        source_codex_home: Path) -> None:
    """Reject lifecycle layouts whose cleanup or sandbox roots can consume secrets."""
    state_root = state_root.expanduser().resolve()
    protected_children = {
        "workspace_root": workspace_root.expanduser().resolve(),
        "codex_home": codex_home.expanduser().resolve(),
        "runner_root": (runner_root or state_root / "runner").expanduser().resolve(),
        "runtime_root": (
            runtime_root or state_root / "provider-runtimes").expanduser().resolve(),
    }
    for label, path in protected_children.items():
        if not _proper_child(path, state_root):
            raise EnrollmentError(
                f"{label} must be a proper child of the protected state root")
    child_items = list(protected_children.items())
    for index, (left_label, left) in enumerate(child_items):
        for right_label, right in child_items[index + 1:]:
            if _paths_overlap(left, right):
                raise EnrollmentError(
                    f"{left_label} and {right_label} must be disjoint")

    config_root = config_root.expanduser().resolve()
    prefix = prefix.expanduser().resolve()
    for label, external in (("config_root", config_root), ("prefix", prefix)):
        if _paths_overlap(external, state_root):
            raise EnrollmentError(f"{label} must be disjoint from the protected state root")
    if _paths_overlap(config_root, prefix):
        raise EnrollmentError("config_root and prefix must be disjoint")

    source_codex_home = source_codex_home.expanduser().resolve()
    for label, lifecycle_root in (
            ("state_root", state_root), ("config_root", config_root),
            ("prefix", prefix)):
        if _paths_overlap(source_codex_home, lifecycle_root):
            raise EnrollmentError(
                f"source_codex_home and {label} must be disjoint")

    log_root = log_root.expanduser().resolve()
    for label, path in protected_children.items():
        if _paths_overlap(log_root, path):
            raise EnrollmentError(f"log_root and {label} must be disjoint")
    if _paths_overlap(log_root, source_codex_home):
        raise EnrollmentError("log_root and source_codex_home must be disjoint")
    service_path = service_path.expanduser().resolve()
    service_protected = {
        "state_root": state_root,
        "config_root": config_root,
        "prefix": prefix,
        "log_root": log_root,
        "source_codex_home": source_codex_home,
        **protected_children,
    }
    for label, path in service_protected.items():
        if _paths_overlap(service_path, path):
            raise EnrollmentError(f"service_path and {label} must be disjoint")


_PENDING_ENROLLMENT_STATUSES = {
    "prepared_for_enrollment",
    "enrollment_retry_required",
    "enrollment_response_incomplete",
}


def _validate_retry_artifacts(
        *, state: dict[str, Any], identity_path: Path, bootstrap_fingerprint: str,
        base_url: str, project: str, target_platform: str, prefix: Path,
        service_path: Path, config_path: Path, state_path: Path,
        workspace_root: Path, codex_home: Path, source_codex_home: Path,
        source_repo_root: Path,
        log_root: Path) -> None:
    """Prove an existing journal is semantically safe before copying local auth."""
    status = str(state.get("status") or "")
    if (state.get("schema") != LOCAL_STATE_SCHEMA
            or status not in (_PENDING_ENROLLMENT_STATUSES | _FINALIZATION_STATUSES)):
        raise EnrollmentError(
            "incomplete existing Agent Host state; revoke or uninstall first")
    expected = {
        "bootstrap_fingerprint": bootstrap_fingerprint,
        "project": project,
        "platform": target_platform,
        "base_url": base_url.rstrip("/"),
        "prefix": str(prefix),
        "service_path": str(service_path),
        "identity_path": str(identity_path),
        "config_path": str(config_path),
        "state_path": str(state_path),
        "source_repo_root": str(source_repo_root),
    }
    if any(str(state.get(key) or "") != str(value) for key, value in expected.items()):
        raise EnrollmentError(
            "existing Agent Host state does not match this enrollment layout")

    if status in _PENDING_ENROLLMENT_STATUSES:
        if not identity_path.is_file() or identity_path.is_symlink():
            raise EnrollmentError(
                "incomplete existing Agent Host state; revoke or uninstall first")
        pending_identity = _read_json(identity_path)
        valid_pending_identity = (
            pending_identity.get("schema") == IDENTITY_SCHEMA
            and pending_identity.get("status") == "pending_enrollment"
            and _private_key_matches_fingerprint(
                str(pending_identity.get("private_key_pem") or ""),
                str(pending_identity.get("public_key_fingerprint") or ""))
            and bool(re.fullmatch(
                r"ahr-[A-Za-z0-9_-]{32,}",
                str(pending_identity.get("completion_recovery_secret") or "")))
        )
        if not valid_pending_identity:
            raise EnrollmentError("pending Agent Host enrollment material is incomplete")
        return

    pending_identity = state.get("pending_identity")
    pending_config = state.get("pending_config")
    if not isinstance(pending_identity, dict) or not isinstance(pending_config, dict):
        raise EnrollmentError("pending enrollment finalization is incomplete")
    valid_final_identity = (
        pending_identity.get("schema") == IDENTITY_SCHEMA
        and bool(str(pending_identity.get("host_id") or ""))
        and bool(str(pending_identity.get("host_token") or ""))
        and bool(str(pending_identity.get("enrollment_id") or ""))
        and _private_key_matches_fingerprint(
            str(pending_identity.get("private_key_pem") or ""),
            str(pending_identity.get("public_key_fingerprint") or ""))
    )
    expected_config = {
        "base_url": base_url.rstrip("/"),
        "project": project,
    }
    expected_config_paths = {
        "service_path": str(service_path),
        "repo_root": str(prefix / "current"),
        "source_repo_root": str(source_repo_root),
        "runner_dir": str(state_path.parent / "runner"),
        "runtime_root": str(state_path.parent / "provider-runtimes"),
        "workspace_root": str(workspace_root),
        "codex_home": str(codex_home),
        "source_codex_home": str(source_codex_home),
        "log_root": str(log_root),
    }
    if not valid_final_identity:
        raise EnrollmentError("pending enrollment finalization identity is incomplete")
    mismatched_config = [
        key for key, value in expected_config.items()
        if str(pending_config.get(key) or "") != str(value)
    ]
    mismatched_config.extend(
        key for key, value in expected_config_paths.items()
        if not str(pending_config.get(key) or "")
        or Path(str(pending_config[key])).expanduser().resolve()
        != Path(value).expanduser().resolve()
    )
    if mismatched_config:
        raise EnrollmentError(
            "pending enrollment finalization configuration mismatches: "
            + ", ".join(mismatched_config))


def _same_resolved_path(left: Path | str, right: Path | str) -> bool:
    return (Path(left).expanduser().resolve()
            == Path(right).expanduser().resolve())


def _validate_persisted_lifecycle_layout(
        *, state: dict[str, Any], state_path: Path, identity_path: Path,
        config_path: Path, config: dict[str, Any]) -> None:
    """Revalidate journal-bound roots before persisted paths drive host effects."""
    if state.get("schema") != LOCAL_STATE_SCHEMA:
        raise EnrollmentError("Agent Host lifecycle state schema is invalid")
    for label, path in (("identity_path", identity_path), ("config_path", config_path)):
        if _lexists(path) and path.is_symlink():
            raise EnrollmentError(f"{label} must not be a symlink")
    bindings = {
        "state_path": state_path,
        "identity_path": identity_path,
        "config_path": config_path,
    }
    for key, path in bindings.items():
        recorded = str(state.get(key) or "").strip()
        if not recorded or not _same_resolved_path(recorded, path):
            raise EnrollmentError(f"{key} does not match the Agent Host journal")

    required_state_paths = {
        key: str(state.get(key) or "").strip()
        for key in ("prefix", "service_path")
    }
    required_config_paths = {
        key: str(config.get(key) or "").strip()
        for key in (
            "service_path", "repo_root", "source_repo_root", "runner_dir", "runtime_root",
            "workspace_root", "codex_home", "source_codex_home", "log_root",
        )
    }
    if (not all(required_state_paths.values())
            or not all(required_config_paths.values())):
        raise EnrollmentError("Agent Host lifecycle paths are incomplete")
    prefix = Path(required_state_paths["prefix"])
    service_path = Path(required_state_paths["service_path"])
    state_root = state_path.parent
    expected_config_paths = {
        "service_path": service_path,
        "repo_root": prefix / "current",
        "runner_dir": state_root / "runner",
        "runtime_root": state_root / "provider-runtimes",
    }
    for key, expected in expected_config_paths.items():
        if not _same_resolved_path(required_config_paths[key], expected):
            raise EnrollmentError(
                f"{key} does not match the Agent Host lifecycle journal")

    source_codex_home = Path(required_config_paths["source_codex_home"])
    if not source_codex_home.is_absolute() or source_codex_home.is_symlink():
        raise EnrollmentError("source_codex_home must be an absolute non-symlink path")
    source_repo_root = Path(required_config_paths["source_repo_root"])
    if not source_repo_root.is_absolute() or source_repo_root.is_symlink():
        raise EnrollmentError("source_repo_root must be an absolute non-symlink path")
    _validate_install_layout(
        prefix=prefix,
        config_root=config_path.parent,
        state_root=state_root,
        workspace_root=Path(required_config_paths["workspace_root"]),
        codex_home=Path(required_config_paths["codex_home"]),
        runner_root=Path(required_config_paths["runner_dir"]),
        runtime_root=Path(required_config_paths["runtime_root"]),
        log_root=Path(required_config_paths["log_root"]),
        service_path=service_path,
        source_codex_home=source_codex_home,
    )


@contextmanager
def _install_lock(*roots: Path):
    """Serialize every shared local enrollment root across processes and threads."""
    lock_paths = set()
    for root in roots:
        normalized = Path(root).expanduser().resolve()
        normalized.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock_paths.add(normalized.parent / f".{normalized.name}.install.lock")
    descriptors = []
    try:
        for lock_path in sorted(lock_paths, key=str):
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise EnrollmentError("Agent Host install lock is unsafe")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _finalize_install(
    *, state: dict[str, Any], state_path: Path, identity_path: Path,
    config_path: Path, target_platform: str, prefix: Path, service_path: Path,
    log_root: Path, state_root: Path, entrypoint: str, start_service: bool,
    http: Callable[..., dict[str, Any]],
    service_runner: Callable[..., subprocess.CompletedProcess],
) -> dict[str, Any]:
    """Idempotently finish a journaled enrollment after server completion."""
    identity = dict(state.get("pending_identity") or {})
    config = dict(state.get("pending_config") or {})
    if (not identity.get("enrollment_id") or not identity.get("host_id")
            or not identity.get("host_token")):
        raise EnrollmentError("pending enrollment finalization lacks host identity material")
    if not config.get("base_url") or not config.get("project"):
        raise EnrollmentError("pending enrollment finalization lacks endpoint configuration")
    source_codex_home_value = str(config.get("source_codex_home") or "").strip()
    if not source_codex_home_value:
        raise EnrollmentError("pending enrollment finalization lacks source Codex home")
    source_repo_root = _validated_source_repo_root(
        str(config.get("source_repo_root") or ""))
    workspace_root = Path(config.get("workspace_root") or state_root / "workspaces")
    codex_home = Path(config.get("codex_home") or state_root / "codex-home")
    _validate_install_layout(
        prefix=prefix,
        config_root=config_path.parent,
        state_root=state_root,
        workspace_root=workspace_root,
        codex_home=codex_home,
        runner_root=Path(config.get("runner_dir") or state_root / "runner"),
        runtime_root=Path(
            config.get("runtime_root") or state_root / "provider-runtimes"),
        log_root=log_root,
        service_path=service_path,
        source_codex_home=Path(source_codex_home_value),
    )
    workspace_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(workspace_root, 0o700)
    if (not codex_home.is_dir() or codex_home.is_symlink()
            or not (codex_home / "auth.json").is_file()
            or (codex_home / "auth.json").is_symlink()):
        raise EnrollmentError("dedicated Codex auth root is not durable")
    os.chmod(codex_home, 0o700)
    os.chmod(codex_home / "auth.json", 0o600)
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
            writable_roots=(state_root, workspace_root, codex_home, source_repo_root),
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
    state.update({
        "status": "installed_finalization_ack_pending",
        "installed_at": time.time(),
        "finalization_step": "local_state_durable",
    })
    _atomic_json(state_path, state, 0o600)
    try:
        acknowledged = http(
            "POST",
            config["base_url"] + "/ixp/v1/agent-host-enrollments/finalize",
            {
                "schema": "switchboard.agent.finalize_host_enrollment_command.v1",
                "project": config["project"],
                "enrollment_id": identity["enrollment_id"],
                "host_id": identity["host_id"],
            },
            identity["host_token"],
        )
    except Exception:
        state["finalization_ack_failed_at"] = time.time()
        _atomic_json(state_path, state, 0o600)
        raise
    if not acknowledged.get("finalized"):
        state["finalization_ack_failed_at"] = time.time()
        _atomic_json(state_path, state, 0o600)
        raise EnrollmentError("Switchboard did not acknowledge enrollment finalization")
    state.pop("pending_identity", None)
    state.pop("pending_config", None)
    state.pop("finalization_attempted_service_start", None)
    state.update({"status": "installed", "finalization_step": "complete",
                  "finalization_acknowledged_at": time.time()})
    _atomic_json(state_path, state, 0o600)
    return {
        "installed": True,
        "host_id": host_id,
        "version": state["version"],
        "state_path": str(state_path),
        "completion_recovered": completion_recovered,
    }


def _install_host_unlocked(*, bundle_dir: Path, public_key_path: Path, bootstrap_code: str,
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
                 source_repo_root: Path | None = None,
                 hostname: str = "") -> dict[str, Any]:
    """Prepare a durable local install, consume bootstrap once, then start."""
    manifest = verify_bundle(bundle_dir, public_key_path)
    bootstrap_code = str(bootstrap_code or "").strip()
    if not bootstrap_code:
        raise EnrollmentError("bootstrap_code is required")
    if target_platform not in manifest.get("platforms", []):
        raise EnrollmentError(f"bundle does not support {target_platform}")
    selected = dict(paths or _default_paths(target_platform))
    prefix = Path(selected["prefix"])
    config_root = Path(selected["config_root"])
    state_root = Path(selected["state_root"])
    workspace_root = Path(selected.get("workspace_root") or state_root / "workspaces")
    codex_home_candidate = Path(
        selected.get("codex_home") or state_root / "codex-home").expanduser()
    try:
        codex_home_candidate.resolve().relative_to(state_root.expanduser().resolve())
    except ValueError as exc:
        raise EnrollmentError(
            "dedicated Codex home must be inside the protected state root") from exc
    service_path = Path(selected["service_path"])
    log_root = Path(selected["log_root"])
    identity_path = config_root / "identity.json"
    config_path = config_root / "config.json"
    state_path = state_root / "state.json"
    source_codex_home = Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()
    operator_source_repo = _validated_source_repo_root(
        source_repo_root or selected.get("source_repo_root") or "")
    source_repo = _provision_host_source_mirror(operator_source_repo, state_root)
    user_home = Path.home().expanduser().resolve()
    _validate_install_layout(
        prefix=prefix,
        config_root=config_root,
        state_root=state_root,
        workspace_root=workspace_root,
        codex_home=codex_home_candidate,
        log_root=log_root,
        service_path=service_path,
        source_codex_home=source_codex_home,
    )
    identity_preexisted = _lexists(identity_path)
    local_state_journaled = False
    codex_home_existed = _lexists(codex_home_candidate)
    codex_auth_existed = _lexists(codex_home_candidate / "auth.json")
    retrying = _lexists(identity_path) or _lexists(state_path)
    existing_state: dict[str, Any] = {}
    bootstrap_fingerprint = "sha256:" + hashlib.sha256(bootstrap_code.encode()).hexdigest()
    if retrying:
        if not state_path.is_file() or state_path.is_symlink():
            raise EnrollmentError(
                "incomplete existing Agent Host state; revoke or uninstall first")
        existing_state = _read_json(state_path)
        _validate_retry_artifacts(
            state=existing_state,
            identity_path=identity_path,
            bootstrap_fingerprint=bootstrap_fingerprint,
            base_url=base_url,
            project=project,
            target_platform=target_platform,
            prefix=prefix,
            service_path=service_path,
            config_path=config_path,
            state_path=state_path,
            workspace_root=workspace_root,
            codex_home=codex_home_candidate,
            source_codex_home=source_codex_home,
            source_repo_root=source_repo,
            log_root=log_root,
        )
        local_state_journaled = True

    def rollback_prejournal_secrets() -> None:
        """Remove fresh-install secrets until state.json makes them resumable."""
        if local_state_journaled:
            return
        candidate = codex_home_candidate.resolve()
        try:
            candidate.relative_to(state_root.expanduser().resolve())
        except ValueError:
            return
        if (not identity_preexisted and identity_path.is_file()
                and not identity_path.is_symlink()):
            identity_path.unlink()
        if (not codex_home_existed and candidate.is_dir()
                and not candidate.is_symlink()):
            shutil.rmtree(candidate)
        elif (codex_home_existed and not codex_auth_existed
              and candidate.is_dir() and not candidate.is_symlink()):
            copied_auth = candidate / "auth.json"
            if copied_auth.is_file() and not copied_auth.is_symlink():
                copied_auth.unlink()

    try:
        codex_home = prepare_dedicated_codex_home(
            codex_home_candidate, source_root=source_codex_home)
        local_auth = preflight_codex_local_auth(
            codex_executable=codex_executable, codex_home=codex_home,
            runner=local_auth_runner)
    except Exception:
        # Nothing can resume or uninstall a credential copy that predates the local
        # lifecycle journal.  On a fresh install, remove the dedicated root at every
        # pre-journal failure boundary; the user's source login is never touched.
        rollback_prejournal_secrets()
        raise
    if retrying:
        state = existing_state
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
            release = _install_release(bundle_dir, manifest, prefix)
            state.update({"version": manifest["version"], "release": str(release),
                          "finalization_resumed_at": time.time()})
            _atomic_json(state_path, state, 0o600)
            return _finalize_install(
                state=state, state_path=state_path, identity_path=identity_path,
                config_path=config_path, target_platform=target_platform, prefix=prefix,
                service_path=service_path, log_root=log_root, state_root=state_root,
                entrypoint=manifest["entrypoint"], start_service=start_service,
                http=http,
                service_runner=service_runner,
            )
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
        release = _install_release(bundle_dir, manifest, prefix)
        state.update({
            "status": "prepared_for_enrollment",
            "version": manifest["version"],
            "release": str(release),
            "retry_started_at": time.time(),
        })
    else:
        try:
            release = _install_release(bundle_dir, manifest, prefix)
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
                "state_path": str(state_path),
                "source_repo_root": str(source_repo),
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
            local_state_journaled = True
        except Exception:
            rollback_prejournal_secrets()
            raise
    if retrying:
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
    if (not enrollment.get("enrollment_id") or not enrollment.get("host_id")
            or not host_token):
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
        "allow_work": bool((enrollment.get("execution_policy") or {}).get(
            "allow_work", allow_work)),
        "allow_global_claim": False,
        "lanes": sorted(set((enrollment.get("execution_policy") or {}).get(
            "lanes") or [])),
        "capabilities": sorted(set((enrollment.get("execution_policy") or {}).get(
            "capabilities") or [])),
        "max_sessions": int((enrollment.get("execution_policy") or {}).get(
            "max_sessions") or 1),
        "personal_wakes_only": bool((enrollment.get("execution_policy") or {}).get(
            "personal_wakes_only", True)),
        "owner_user_id": str(enrollment.get("owner_user_id") or ""),
        "tenant_allowlist": sorted(set(enrollment.get("tenant_allowlist") or [])),
        "project_allowlist": enrollment.get("project_allowlist") or [project],
        "provider_allowlist": sorted(set(enrollment.get("provider_allowlist") or [])),
        "local_auth_account_proof": local_auth["account_fingerprint"],
        "codex_executable": local_auth["codex_executable"],
        "platform": target_platform,
        "service_path": str(service_path),
        "repo_root": str(prefix / "current"),
        "source_repo_root": str(source_repo),
        "runner_dir": str(state_root / "runner"),
        "runtime_root": str(state_root / "provider-runtimes"),
        "workspace_root": str(workspace_root),
        "codex_home": str(codex_home),
        "source_codex_home": str(source_codex_home),
        "log_root": str(log_root),
        "user_home": str(user_home),
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
        http=http,
        service_runner=service_runner,
    )


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
                 codex_executable: str = "", source_repo_root: Path | None = None,
                 hostname: str = "") -> dict[str, Any]:
    """Serialize enrollment from local-state detection through finalization."""
    selected = dict(paths or _default_paths(target_platform))
    with _install_lock(
            Path(selected["state_root"]), Path(selected["prefix"]),
            Path(selected["config_root"])):
        return _install_host_unlocked(
            bundle_dir=bundle_dir,
            public_key_path=public_key_path,
            bootstrap_code=bootstrap_code,
            base_url=base_url,
            project=project,
            owner_user_id=owner_user_id,
            target_platform=target_platform,
            paths=selected,
            allow_work=allow_work,
            lanes=lanes,
            tenant_allowlist=tenant_allowlist,
            provider_allowlist=provider_allowlist,
            start_service=start_service,
            http=http,
            service_runner=service_runner,
            local_auth_runner=local_auth_runner,
            codex_executable=codex_executable,
            source_repo_root=source_repo_root,
            hostname=hostname,
        )


def update_host(*, bundle_dir: Path, public_key_path: Path, state_path: Path,
                source_repo_root: Path | None = None,
                restart_service: bool = True,
                service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    manifest = verify_bundle(bundle_dir, public_key_path)
    state = _read_json(state_path)
    if (state.get("status") != "installed"
            or state.get("remote_revocation_confirmed") is True
            or state.get("revocation_requested_at")
            or state.get("revocation_identity")
            or state.get("revocation_config")):
        raise EnrollmentError(
            "Agent Host update requires a clean installed state with no pending revocation")
    if _parse_version(manifest["version"]) <= _parse_version(state.get("version") or ""):
        raise EnrollmentError("update bundle must be newer than the installed version")
    prefix = Path(state["prefix"])
    config_path = Path(state["config_path"])
    config = _read_json(config_path)
    previous_config = dict(config)
    selected_source = _validated_source_repo_root(
        source_repo_root or config.get("source_repo_root") or "")
    source_repo = _provision_host_source_mirror(selected_source, state_path.parent)
    current = prefix / "current"
    previous = current.resolve() if current.exists() else None
    release = _install_release(bundle_dir, manifest, prefix)

    def render(config_value: dict[str, Any]) -> None:
        roots = [
            state_path.parent,
            Path(config_value["workspace_root"]),
            Path(config_value["codex_home"]),
        ]
        previous_source = str(config_value.get("source_repo_root") or "").strip()
        if previous_source:
            roots.append(_validated_source_repo_root(previous_source))
        render_service(
            state["platform"], python=sys.executable,
            entrypoint=prefix / "current" / manifest["entrypoint"],
            identity_path=Path(state["identity_path"]), config_path=config_path,
            service_path=Path(state["service_path"]),
            log_root=Path(config_value["log_root"]), writable_roots=roots)

    try:
        config["agent_host_version"] = manifest["version"]
        config["source_repo_root"] = str(source_repo)
        _atomic_json(config_path, config, 0o600)
        render(config)
        if restart_service:
            control_service(
                state["platform"], "restart", Path(state["service_path"]), runner=service_runner)
    except Exception as update_error:
        if previous:
            rollback = prefix / f".current.rollback.{os.getpid()}"
            rollback.symlink_to(previous)
            os.replace(rollback, current)
        _atomic_json(config_path, previous_config, 0o600)
        render(previous_config)
        if restart_service:
            try:
                control_service(
                    state["platform"], "restart", Path(state["service_path"]),
                    runner=service_runner)
            except Exception as rollback_error:
                raise EnrollmentError(
                    "Agent Host update failed and the rolled-back service could not restart: "
                    f"{rollback_error}") from update_error
        raise
    state.update({"version": manifest["version"], "release": str(release),
                  "status": "installed", "updated_at": time.time()})
    _atomic_json(state_path, state, 0o600)
    return {"updated": True, "version": manifest["version"]}


def _require_lifecycle_project(project: str, *records: dict[str, Any]) -> str:
    """Bind a local lifecycle mutation to one explicit persisted project."""
    requested = str(project or "").strip()
    if not requested:
        raise EnrollmentError("project is required")
    persisted = {
        str(record.get("project") or "").strip()
        for record in records if isinstance(record, dict) and record.get("project")
    }
    if not persisted:
        raise EnrollmentError("installed lifecycle state lacks project scope")
    if persisted != {requested}:
        raise EnrollmentError("requested project does not match installed lifecycle state")
    return requested


def rotate_identity(*, identity_path: Path, config_path: Path, project: str,
                    http: Callable[..., dict[str, Any]] = request_json,
                    service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                    ) -> dict[str, Any]:
    identity = _read_json(identity_path)
    config = _read_json(config_path)
    project = _require_lifecycle_project(project, config)
    target_platform = str(config.get("platform") or "").strip()
    service_path = str(config.get("service_path") or "").strip()
    if target_platform not in {"darwin", "linux"} or not service_path:
        raise EnrollmentError(
            "installed configuration lacks the service restart binding")
    if identity.get("rotation_pending_restart"):
        # A previous rotation already persisted the only live bearer but service
        # start failed or its response was lost. Resume that exact boundary without
        # rotating again. Restart covers an already-running ambiguous outcome;
        # start covers a service that is still unloaded.
        try:
            control_service(
                target_platform, "restart", Path(service_path), runner=service_runner)
        except EnrollmentError:
            control_service(
                target_platform, "start", Path(service_path), runner=service_runner)
        identity.pop("rotation_pending_restart", None)
        identity["rotation_restarted_at"] = time.time()
        _atomic_json(identity_path, identity, 0o600)
        return {
            "rotated": True,
            "identity_generation": identity.get("identity_generation"),
            "service_restarted": True,
            "resumed": True,
        }
    # Stop the old-token daemon before invalidating its bearer. If stop cannot be
    # established, fail before the remote identity changes.
    control_service(
        target_platform, "stop", Path(service_path), runner=service_runner)
    private_key_pem, fingerprint = generate_host_identity()
    result = http(
        "POST",
        config["base_url"] + "/ixp/v1/agent-host-enrollments/rotate",
        {"project": project, "host_id": identity["host_id"],
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
        "rotation_pending_restart": True,
    })
    _atomic_json(identity_path, identity, 0o600)
    control_service(
        target_platform, "start", Path(service_path), runner=service_runner)
    identity.pop("rotation_pending_restart", None)
    identity["rotation_restarted_at"] = time.time()
    _atomic_json(identity_path, identity, 0o600)
    return {"rotated": True, "identity_generation": identity["identity_generation"],
            "service_restarted": True}


_CO6_CANONICAL_PROVIDERS = ("openai-codex", "anthropic-claude", "cursor")


def declare_account_affinity(*, identity_path: Path, config_path: Path, project: str,
                             provider: str, account_id: str,
                             service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                             ) -> dict[str, Any]:
    """Locally declare the CO-6 account fingerprint this host attests for a provider.

    Pure local filesystem operation — no network call, no server-side write path.
    Only someone with shell access to this same user account can write this file;
    the server only ever learns its contents through the daemon's own already-
    authenticated heartbeat (same trust boundary as the legacy PM_HOST_ACCOUNT_AFFINITIES
    env var this sits alongside — see placement_inventory() in agent_host.py). A Settings
    "Bind this connection" click cannot reach this file and cannot forge an affinity.
    """
    identity = _read_json(identity_path)
    config = _read_json(config_path)
    project = _require_lifecycle_project(project, identity, config)
    provider = str(provider or "").strip().lower()
    if provider not in _CO6_CANONICAL_PROVIDERS:
        raise EnrollmentError(f"provider must be one of {list(_CO6_CANONICAL_PROVIDERS)}")
    account_id = str(account_id or "").strip()
    if not account_id:
        raise EnrollmentError("account_id is required")
    fingerprint = "acct-" + hashlib.sha256(
        f"{provider}\x1f{account_id}".encode("utf-8")).hexdigest()[:16]
    declarations_path = config_path.parent / ACCOUNT_AFFINITIES_FILENAME
    current: dict[str, Any] = {}
    if declarations_path.is_file() and not declarations_path.is_symlink():
        current = _read_json(declarations_path)
    existing = {
        str(item).strip() for item in current.get(ACCOUNT_AFFINITY_IDS_KEY) or []
        if str(item or "").strip()
    }
    already_declared = fingerprint in existing
    existing.add(fingerprint)
    _atomic_json(declarations_path, {
        "schema": "switchboard.agent_host_account_affinities.v1",
        ACCOUNT_AFFINITY_IDS_KEY: sorted(existing),
        "declared_at": time.time(),
    }, 0o600)
    target_platform = str(config.get("platform") or "").strip()
    service_path = str(config.get("service_path") or "").strip()
    restarted = False
    warning = ""
    if already_declared:
        # Re-declaring the same fingerprint changes nothing on disk in effect —
        # restarting a live, possibly mid-wake daemon for a pure no-op is exactly
        # the kind of restart this mechanism should not reach for reflexively.
        warning = "this account was already declared; no restart was needed"
    elif target_platform in {"darwin", "linux"} and service_path:
        # placement_inventory() is computed once at daemon start, so the fingerprint
        # only reaches the next heartbeat after a restart — mirrors rotate_identity's
        # own restart-to-apply pattern.
        control_service(
            target_platform, "restart", Path(service_path), runner=service_runner)
        restarted = True
    else:
        warning = (
            "could not restart the service automatically (platform/service_path "
            "unavailable) — restart it yourself for this declaration to take effect"
        )
    result = {
        "declared": True,
        "provider": provider,
        "account_fingerprint": fingerprint,
        "service_restarted": restarted,
    }
    if warning:
        result["warning"] = warning
    return result


def enroll_api_key(*, identity_path: Path, config_path: Path, project: str,
                   provider: str, provider_account_id: str,
                   billing_account_id: str, budget_ceiling: float,
                   budget_currency: str, api_key: str,
                   http: Callable[..., dict[str, Any]] = request_json,
                   ) -> dict[str, Any]:
    """Submit one API key from the owner host without argv, env, or disk residue."""
    identity_path = Path(identity_path)
    config_path = Path(config_path)
    if (not identity_path.is_file() or identity_path.is_symlink()
            or stat.S_IMODE(identity_path.stat().st_mode) & 0o077):
        raise EnrollmentError("identity file must be a regular 0600 file")
    if not config_path.is_file() or config_path.is_symlink():
        raise EnrollmentError("config file must be a regular file")
    identity = _read_json(identity_path)
    config = _read_json(config_path)
    project = _require_lifecycle_project(project, identity, config)
    provider = str(provider or "").strip().lower()
    if provider != "openai-codex":
        raise EnrollmentError("only openai-codex API enrollment is enabled")
    provider_account_id = str(provider_account_id or "").strip()
    billing_account_id = str(billing_account_id or "").strip()
    currency = str(budget_currency or "").strip().upper()
    try:
        ceiling = float(budget_ceiling)
    except (TypeError, ValueError) as exc:
        raise EnrollmentError("budget ceiling must be a positive finite number") from exc
    if (not provider_account_id or not billing_account_id
            or not math.isfinite(ceiling) or ceiling <= 0):
        raise EnrollmentError("provider account, billing account, and positive finite budget are required")
    if len(currency) != 3 or not currency.isalpha():
        raise EnrollmentError("budget currency must be a three-letter code")
    secret = str(api_key or "").rstrip("\r\n")
    if not secret:
        raise EnrollmentError("API key is required on stdin")
    payload = {
        "project": project,
        "host_id": identity.get("host_id"),
        "provider": provider,
        "provider_account_id": provider_account_id,
        "billing_account_id": billing_account_id,
        "budget_ceiling": ceiling,
        "budget_currency": currency,
        "api_key": secret,
    }
    try:
        result = http(
            "POST",
            config["base_url"].rstrip("/")
            + "/ixp/v1/agent-host-provider-connections/enroll-api-key",
            payload,
            identity.get("host_token") or "",
        )
    finally:
        payload["api_key"] = ""
        secret = ""
    reference = str(
        result.get("execution_connection_id")
        or result.get("credential_reference") or "").strip()
    if not reference:
        raise EnrollmentError("Switchboard did not return an API execution connection")
    return {
        "enrolled": True,
        "provider": provider,
        "connection_kind": result.get("connection_kind") or "direct_api",
        "execution_connection_id": reference,
        "billing_account_bound": bool(result.get("billing_account_bound")),
        "budget_policy": result.get("budget_policy") or {},
        "credential_present": bool(result.get("credential_present", True)),
        "credential_values_redacted": True,
    }


def revoke_host(*, identity_path: Path, config_path: Path, state_path: Path, project: str,
                final_status: str = "revoked", stop_service: bool = True,
                http: Callable[..., dict[str, Any]] = request_json,
                service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    """Journal remote revocation and resume local cleanup across every boundary."""
    identity_path = Path(identity_path)
    config_path = Path(config_path)
    state_path = Path(state_path)
    if not state_path.is_file() or state_path.is_symlink():
        raise EnrollmentError("Agent Host lifecycle state must be a regular file")
    state = _read_json(state_path)
    # Reject a cross-project request before even advancing the local lifecycle journal.
    project = _require_lifecycle_project(project, state)
    operation = "uninstall" if final_status == "uninstalled" else "revoke"
    recorded_operation = str(state.get("revocation_operation") or "")
    if recorded_operation and recorded_operation != operation:
        completed_revoke_to_uninstall = (
            recorded_operation == "revoke" and operation == "uninstall"
            and state.get("remote_revocation_confirmed") is True
            and state.get("status") == "revoked"
        )
        if not completed_revoke_to_uninstall:
            raise EnrollmentError("a different host lifecycle operation is already pending")
    identity = dict(state.get("revocation_identity") or {})
    config = dict(state.get("revocation_config") or {})
    if not identity:
        if _lexists(identity_path) and identity_path.is_symlink():
            raise EnrollmentError("identity_path must not be a symlink")
        identity = (_read_json(identity_path) if identity_path.is_file()
                    else dict(state.get("pending_identity") or {}))
    if not identity.get("host_token"):
        identity = dict(state.get("pending_identity") or identity)
    if not config:
        if _lexists(config_path) and config_path.is_symlink():
            raise EnrollmentError("config_path must not be a symlink")
        config = (_read_json(config_path) if config_path.is_file()
                  else dict(state.get("pending_config") or {}))
    if not config.get("base_url"):
        config = dict(state.get("pending_config") or config)
    project = _require_lifecycle_project(project, state, config)
    _validate_persisted_lifecycle_layout(
        state=state,
        state_path=state_path,
        identity_path=identity_path,
        config_path=config_path,
        config=config,
    )
    remote_confirmed = state.get("remote_revocation_confirmed") is True
    state.update({
        "revocation_operation": operation,
        "revocation_identity": identity,
        "revocation_config": config,
    })
    if not remote_confirmed:
        if not identity.get("host_id") or not identity.get("host_token"):
            raise EnrollmentError("no completed host identity is available to revoke")
        if not config.get("base_url") or not config.get("project"):
            raise EnrollmentError("no enrollment endpoint is available to revoke the host")
        state.update({
            "status": "revocation_requested",
            "revocation_requested_at": state.get("revocation_requested_at") or time.time(),
        })
    _atomic_json(state_path, state, 0o600)
    if not remote_confirmed:
        if stop_service:
            control_service(
                state["platform"], "stop", Path(state["service_path"]),
                runner=service_runner)
        try:
            result = http(
                "POST",
                config["base_url"] + "/ixp/v1/agent-host-enrollments/revoke",
                {"project": project, "host_id": identity["host_id"],
                 "reason": "local_host_revoke", "final_status": final_status},
                identity["host_token"],
            )
        except Exception:
            state.update({"status": "revocation_response_unknown",
                          "revocation_response_unknown_at": time.time()})
            _atomic_json(state_path, state, 0o600)
            raise
        if not result.get("revoked"):
            raise EnrollmentError("Switchboard did not confirm host revocation")
        state.update({
            "status": "remote_revocation_confirmed",
            "remote_revocation_confirmed": True,
            "remote_revocation_confirmed_at": time.time(),
            "post_revoke_denial": True,
        })
        _atomic_json(state_path, state, 0o600)

    state["status"] = "local_cleanup_pending"
    if state.get("platform") == "darwin":
        # `launchctl bootout` unloads only the current login session.  Removing the
        # per-user plist makes revoke durable across the next login as well.
        Path(state["service_path"]).unlink(missing_ok=True)
        state["cleanup_step"] = "service_disabled"
        _atomic_json(state_path, state, 0o600)
    identity_path.unlink(missing_ok=True)
    state["cleanup_step"] = "identity_deleted"
    _atomic_json(state_path, state, 0o600)
    runtime_root_value = str(config.get("runtime_root") or "").strip()
    if runtime_root_value:
        runtime_root = Path(runtime_root_value)
        if runtime_root.is_dir():
            shutil.rmtree(runtime_root)
    codex_home_value = str(config.get("codex_home") or "").strip()
    if codex_home_value:
        codex_home = Path(codex_home_value)
        if codex_home.is_dir() and not codex_home.is_symlink():
            shutil.rmtree(codex_home)
    state["cleanup_step"] = "runtime_residue_deleted"
    _atomic_json(state_path, state, 0o600)
    if operation == "uninstall":
        Path(state["service_path"]).unlink(missing_ok=True)
        state["cleanup_step"] = "service_deleted"
        _atomic_json(state_path, state, 0o600)
        prefix = Path(state["prefix"])
        if prefix.exists():
            shutil.rmtree(prefix)
        state["cleanup_step"] = "releases_deleted"
        _atomic_json(state_path, state, 0o600)
        config_root = config_path.parent
        if config_root.exists():
            shutil.rmtree(config_root)
        state["cleanup_step"] = "config_deleted"
        _atomic_json(state_path, state, 0o600)
        state_root = state_path.parent
        if state_root.exists():
            shutil.rmtree(state_root)
        return {"revoked": True, "status": final_status, "uninstalled": True}

    state.update({"status": "revoked", "revoked_at": time.time(),
                  "cleanup_step": "complete"})
    for key in ("pending_identity", "pending_config", "revocation_identity",
                "revocation_config"):
        state.pop(key, None)
    _atomic_json(state_path, state, 0o600)
    return {"revoked": True, "status": final_status}


def uninstall_host(*, identity_path: Path, config_path: Path, state_path: Path, project: str,
                   http: Callable[..., dict[str, Any]] = request_json,
                   service_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    return revoke_host(
        identity_path=identity_path,
        config_path=config_path,
        state_path=state_path,
        project=project,
        final_status="uninstalled",
        http=http,
        service_runner=service_runner,
    )


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
    codex_home = Path(str(config.get("codex_home") or "")).expanduser()
    if (not str(config.get("codex_home") or "").strip()
            or not codex_home.is_dir() or codex_home.is_symlink()
            or not (codex_home / "auth.json").is_file()
            or (codex_home / "auth.json").is_symlink()):
        raise EnrollmentError("dedicated Codex auth root is unavailable")
    source_codex_home = Path(
        str(config.get("source_codex_home") or "")).expanduser()
    user_home = Path(str(config.get("user_home") or "")).expanduser()
    if (not source_codex_home.is_absolute() or source_codex_home.is_symlink()
            or not user_home.is_absolute() or not user_home.is_dir()
            or user_home.is_symlink()):
        raise EnrollmentError("host credential-isolation roots are unavailable")
    local_auth = preflight_codex_local_auth(
        codex_executable=str(config.get("codex_executable") or ""),
        codex_home=codex_home)
    source_repo_root = _validated_source_repo_root(
        str(config.get("source_repo_root") or ""))
    values = {
        "PM_BASE": config["base_url"],
        # Watch/Chat is a two-hop stream: the Mac PTY binds on loopback, then the
        # Agent Host opens a host tunnel to Switchboard and gives the browser a
        # public wss:// relay URL.  Without this binding supervisor_action("open")
        # can only return 127.0.0.1, which the browser correctly rejects.
        "PM_SWITCHBOARD_PUBLIC_BASE": config["base_url"],
        "PM_RUNNER_PTY_RELAY_PUBLIC_BASE": config["base_url"],
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
        "PM_HOST_CAPABILITIES": ",".join(config.get("capabilities") or []),
        "PM_HOST_MAX_SESSIONS": str(config.get("max_sessions") or 1),
        "PM_PERSONAL_AGENT_HOST_EXECUTION": "1" if config.get("personal_wakes_only") else "0",
        "PM_PERSONAL_AGENT_HOST_RECOVERY": "1",
        "PM_PERSONAL_WORKSPACE_ROOT": config.get("workspace_root") or "",
        "PM_HOST_OWNER_USER_ID": config.get("owner_user_id") or "",
        "PM_HOST_TENANTS": ",".join(config.get("tenant_allowlist") or []),
        "PM_HOST_PROJECTS": ",".join(config.get("project_allowlist") or [config["project"]]),
        "PM_HOST_PROVIDERS": ",".join(config.get("provider_allowlist") or []),
        # Enrollment is itself the server-attested trusted-private boundary: the
        # host is user-owned, has an isolated native Codex auth root, and can only
        # advertise the persisted server-issued policy.  Mark that boundary
        # explicitly so provider-native connection binding does not downgrade a
        # correctly enrolled host to the generic managed/user-owned class merely
        # because it does not consume centrally leased credentials.
        "PM_AUTH_HOST_CLASSES": "trusted_private_worker,user_owned_persistent",
        "PM_HOST_LOCAL_AUTH_AVAILABLE": "1" if local_auth.get("authenticated") else "0",
        "PM_HOST_LOCAL_AUTH_MODE": local_auth.get("auth_mode") or "unavailable",
        "PM_HOST_LOCAL_AUTH_ACCOUNT_PROOF": local_auth.get("account_fingerprint") or "",
        "PM_CODEX_EXECUTABLE": local_auth.get("codex_executable") or "",
        "PM_REPO_ROOT": config["repo_root"],
        "PM_AGENT_HOST_SOURCE_REPO_ROOT": str(source_repo_root),
        "PM_RUNNER_DIR": config["runner_dir"],
        "PM_PROVIDER_RUNTIME_ROOT": config["runtime_root"],
        "PM_AGENT_HOST_VERSION": config.get("agent_host_version") or AGENT_HOST_VERSION,
        "PM_AGENT_HOST_PLATFORM": config.get("platform") or _platform(),
        "PM_AGENT_HOST_IDENTITY_PATH": str(identity_path.resolve()),
        "PM_AGENT_HOST_CONFIG_PATH": str(config_path.resolve()),
        "PM_AGENT_HOST_STATE_PATH": str(
            (Path(config["runner_dir"]).parent / "state.json").resolve()),
        "PM_AGENT_HOST_RUNNER_DIR": str(Path(config["runner_dir"]).resolve()),
        "PM_AGENT_HOST_RUNTIME_ROOT": str(Path(config["runtime_root"]).resolve()),
        "PM_AGENT_HOST_CODEX_HOME": str(codex_home.resolve()),
        "PM_AGENT_HOST_SOURCE_CODEX_HOME": str(source_codex_home.resolve()),
        "PM_AGENT_HOST_USER_HOME": str(user_home.resolve()),
        "CODEX_HOME": str(codex_home.resolve()),
    }
    env.update({key: str(value) for key, value in values.items()})
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(config["repo_root"]) / "src"),
        str(Path(config["repo_root"])),
        env.get("PYTHONPATH", ""),
    )))
    daemon = Path(config["repo_root"]) / "adapters" / "agent_host.py"
    # Durable controls are the recovery path when the live PTY relay is absent.
    # Ten-second polling made a normal Watch reopen/chat fallback take 10-30s;
    # two seconds keeps recovery responsive without turning this into a busy loop.
    os.execve(sys.executable, [sys.executable, str(daemon), "--interval", "2"], env)


def _platform(value: str = "") -> str:
    result = (value or platform_module.system()).strip().lower()
    if result in {"macos", "mac", "darwin"}:
        return "darwin"
    if result == "linux":
        return "linux"
    raise EnrollmentError("Agent Host enrollment supports macOS and Linux")


def _non_blank_project(value: str) -> str:
    project = str(value or "").strip()
    if not project:
        raise argparse.ArgumentTypeError("--project requires a non-blank value")
    return project


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
    install.add_argument("--project", required=True, type=_non_blank_project)
    install.add_argument("--owner-user-id", required=True)
    install.add_argument("--source-repo-root", type=Path, required=True)
    install.add_argument("--platform", default="")
    install.add_argument("--lanes", default="")
    install.add_argument("--no-start", action="store_true")
    update = sub.add_parser("update")
    update.add_argument("--bundle", type=Path, required=True)
    update.add_argument("--public-key", type=Path, required=True)
    update.add_argument("--state", type=Path, required=True)
    update.add_argument("--source-repo-root", type=Path)
    update.add_argument("--no-restart", action="store_true")
    rotate = sub.add_parser("rotate")
    rotate.add_argument("--identity", type=Path, required=True)
    rotate.add_argument("--config", type=Path, required=True)
    rotate.add_argument("--project", required=True, type=_non_blank_project)
    declare = sub.add_parser("declare-account")
    declare.add_argument("--identity", type=Path, required=True)
    declare.add_argument("--config", type=Path, required=True)
    declare.add_argument("--project", required=True, type=_non_blank_project)
    declare.add_argument("--provider", required=True, choices=list(_CO6_CANONICAL_PROVIDERS))
    declare.add_argument("--account-id", required=True)
    api_key = sub.add_parser("enroll-api-key")
    api_key.add_argument("--identity", type=Path, required=True)
    api_key.add_argument("--config", type=Path, required=True)
    api_key.add_argument("--project", required=True, type=_non_blank_project)
    api_key.add_argument("--provider", required=True, choices=["openai-codex"])
    api_key.add_argument("--provider-account", default="")
    api_key.add_argument("--billing-account", required=True)
    api_key.add_argument("--budget-ceiling", required=True, type=float)
    api_key.add_argument("--budget-currency", default="usd")
    api_key.add_argument("--api-key-stdin", action="store_true", required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--identity", type=Path, required=True)
    revoke.add_argument("--config", type=Path, required=True)
    revoke.add_argument("--state", type=Path, required=True)
    revoke.add_argument("--project", required=True, type=_non_blank_project)
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--identity", type=Path, required=True)
    uninstall.add_argument("--config", type=Path, required=True)
    uninstall.add_argument("--state", type=Path, required=True)
    uninstall.add_argument("--project", required=True, type=_non_blank_project)
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
                source_repo_root=args.source_repo_root,
                target_platform=_platform(args.platform),
                lanes=[item.strip() for item in args.lanes.split(",") if item.strip()],
                start_service=not args.no_start,
            )
        elif args.command == "update":
            result = update_host(
                bundle_dir=args.bundle,
                public_key_path=args.public_key,
                state_path=args.state,
                source_repo_root=args.source_repo_root,
                restart_service=not args.no_restart,
            )
        elif args.command == "rotate":
            result = rotate_identity(
                identity_path=args.identity, config_path=args.config, project=args.project)
        elif args.command == "declare-account":
            result = declare_account_affinity(
                identity_path=args.identity, config_path=args.config, project=args.project,
                provider=args.provider, account_id=args.account_id)
        elif args.command == "enroll-api-key":
            secret = sys.stdin.readline().rstrip("\r\n")
            try:
                result = enroll_api_key(
                    identity_path=args.identity, config_path=args.config,
                    project=args.project, provider=args.provider,
                    provider_account_id=args.provider_account or args.billing_account,
                    billing_account_id=args.billing_account,
                    budget_ceiling=args.budget_ceiling,
                    budget_currency=args.budget_currency, api_key=secret)
            finally:
                secret = ""
        elif args.command == "revoke":
            result = revoke_host(
                identity_path=args.identity, config_path=args.config, state_path=args.state,
                project=args.project)
        elif args.command == "uninstall":
            result = uninstall_host(
                identity_path=args.identity, config_path=args.config, state_path=args.state,
                project=args.project)
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
