#!/usr/bin/env python3
"""ADAPTER-18: signed macOS/Linux enrollment, rotation, revoke, and residue proof."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch


TMP = Path(tempfile.mkdtemp(prefix="adapter18-agent-host-enrollment-"))
TEST_CODEX = TMP / "bin" / "codex"
TEST_CODEX.parent.mkdir()
TEST_CODEX.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
TEST_CODEX.chmod(0o755)
TEST_CODEX = TEST_CODEX.resolve()
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import store  # noqa: E402
from switchboard.storage.repositories import agent_host_enrollments as enrollment_store  # noqa: E402
from adapters import agent_host_enrollment as enrollment  # noqa: E402
from adapters import codex_local_worker, codex_personal_worker, switchboard_core  # noqa: E402
from app import app  # noqa: E402
from switchboard.application.commands import complete_claim as complete_claim_command  # noqa: E402
from switchboard.application.commands import complete_wake as complete_wake_command  # noqa: E402
from switchboard.application.commands import work_sessions as work_session_commands  # noqa: E402


ROOT = Path(__file__).resolve().parent
PROJECT = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def paths(name: str) -> dict[str, Path]:
    root = TMP / name
    return {
        "prefix": root / "prefix",
        "config_root": root / "config",
        "state_root": root / "state",
        "log_root": root / "logs",
        "service_path": root / "service" / (
            "agent-host.plist" if "mac" in name else "agent-host.service"),
    }


client = TestClient(app)


def http(method, url, body, token="", timeout=30):
    del timeout
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.request(method, urlsplit(url).path, json=body, headers=headers)
    value = response.json()
    if response.status_code >= 400:
        message = value.get("detail") if isinstance(value, dict) else str(value)
        raise enrollment.EnrollmentError(str(message))
    return value


service_calls: list[list[str]] = []
codex_calls: list[list[str]] = []


def fake_service(command, **kwargs):
    del kwargs
    service_calls.append(list(command))
    return subprocess.CompletedProcess(command, 0, "", "")


def fake_codex(command, **kwargs):
    environment = kwargs.get("env") or {}
    codex_calls.append(list(command))
    ok("OPENAI_API_KEY" not in environment,
       "local-auth preflight strips inherited metered provider credentials")
    output = "codex-cli 1.2.3\n" if command[-1] == "--version" else "Logged in using ChatGPT\n"
    return subprocess.CompletedProcess(command, 0, output, "")


def fake_api_key_codex(command, **kwargs):
    del kwargs
    output = ("codex-cli 1.2.3\n" if command[-1] == "--version"
              else "Logged in using an API key\n")
    return subprocess.CompletedProcess(command, 0, output, "")


def begin(host_id: str):
    response = client.post("/ixp/v1/agent-host-enrollments", json={
        "project": PROJECT,
        "owner_user_id": "user-adapter18",
        "requested_host_id": host_id,
        "tenant_allowlist": ["tenant-adapter18"],
        "project_allowlist": [PROJECT],
        "provider_allowlist": ["openai-codex"],
        "package_version": "0.2.0",
        "ttl_seconds": 300,
    })
    ok(response.status_code == 200 and response.json().get("created") is True,
       f"Switchboard issues one short-lived bootstrap for {host_id}")
    return response.json()


try:
    store.init_db(PROJECT)
    secure_target = TMP / "atomic-proof" / "identity.json"
    secure_target.parent.mkdir()
    stale_temp = secure_target.parent / ".identity.json.stale.tmp"
    stale_temp.write_text("stale secret")
    os.utime(stale_temp, (0, 0))
    protected_target = TMP / "must-not-delete"
    protected_target.write_text("protected")
    stale_symlink = secure_target.parent / ".identity.json.symlink.tmp"
    stale_symlink.symlink_to(protected_target)
    observed_atomic_modes = []
    original_json_dump = enrollment.json.dump

    def inspect_atomic_mode(value, target, **kwargs):
        observed_atomic_modes.append(stat.S_IMODE(os.fstat(target.fileno()).st_mode))
        return original_json_dump(value, target, **kwargs)

    original_umask = os.umask(0o022)
    try:
        with patch.object(enrollment.json, "dump", side_effect=inspect_atomic_mode):
            enrollment._atomic_json(secure_target, {"host_token": "secret"})
    finally:
        os.umask(original_umask)
    ok(observed_atomic_modes == [0o600]
       and stat.S_IMODE(secure_target.stat().st_mode) == 0o600
       and stat.S_IMODE(secure_target.parent.stat().st_mode) == 0o700
       and not stale_temp.exists() and stale_symlink.is_symlink()
       and protected_target.read_text() == "protected",
       "atomic credential writes are 0600 before content, clean stale regular files, and ignore symlinks")
    try:
        enrollment.preflight_codex_local_auth(
            codex_executable=str(TEST_CODEX), runner=fake_api_key_codex)
        api_key_mode_denied = False
    except enrollment.EnrollmentError:
        api_key_mode_denied = True
    ok(api_key_mode_denied,
       "local-auth preflight rejects a stored API-key login as non-personal auth")
    private_key = Ed25519PrivateKey.generate()
    private_path = TMP / "bundle-signing-private.pem"
    public_path = TMP / "bundle-signing-public.pem"
    private_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    public_path.write_bytes(private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    bundle_020 = TMP / "bundle-0.2.0"
    bundle_021 = TMP / "bundle-0.2.1"
    manifest = enrollment.create_signed_bundle(ROOT, bundle_020, "0.2.0", private_path)
    verified = enrollment.verify_bundle(bundle_020, public_path)
    ok(verified["version"] == "0.2.0" and len(manifest["files"]) > 20,
       "signed bundle covers the Agent Host runtime and verifies exactly")

    first_payload = bundle_020 / "payload" / manifest["files"][0]["path"]
    original = first_payload.read_bytes()
    first_payload.write_bytes(original + b"tampered")
    try:
        enrollment.verify_bundle(bundle_020, public_path)
        tamper_denied = False
    except enrollment.EnrollmentError:
        tamper_denied = True
    first_payload.write_bytes(original)
    ok(tamper_denied, "tampered bundle is denied before bootstrap consumption")

    unsigned_source = TMP / "unsigned-bundle-content"
    unsigned_source.mkdir()
    (unsigned_source / "cryptography.py").write_text(
        "raise RuntimeError('unsigned code executed')\n", encoding="utf-8")
    unsigned_link = bundle_020 / "payload" / "adapters" / "unsigned-runtime"
    unsigned_link.symlink_to(unsigned_source, target_is_directory=True)
    try:
        enrollment.verify_bundle(bundle_020, public_path)
        symlink_denied = False
    except enrollment.EnrollmentError:
        symlink_denied = True
    try:
        enrollment._install_release(
            bundle_020, manifest, TMP / "unsigned-copy-prefix")
        symlink_copy_denied = False
    except enrollment.EnrollmentError:
        symlink_copy_denied = True
    unsigned_link.unlink()
    ok(symlink_denied and symlink_copy_denied,
       "signed-bundle verification and copy reject undeclared directory symlinks")

    durability_bootstrap = begin("host/adapter18-durability")
    durability_paths = paths("durability-failure")
    durability_paths["config_root"].parent.mkdir(parents=True, exist_ok=True)
    durability_paths["config_root"].write_text("blocks identity directory", encoding="utf-8")
    durability_http_calls = []

    def durability_http(*args, **kwargs):
        durability_http_calls.append((args, kwargs))
        return http(*args, **kwargs)

    try:
        enrollment.install_host(
            bundle_dir=bundle_020,
            public_key_path=public_path,
            bootstrap_code=durability_bootstrap["bootstrap_code"],
            base_url="https://switchboard.test",
            project=PROJECT,
            owner_user_id="user-adapter18",
            target_platform="linux",
            paths=durability_paths,
            http=durability_http,
            service_runner=fake_service,
            local_auth_runner=fake_codex,
            codex_executable=str(TEST_CODEX),
            start_service=False,
        )
        durability_denied = False
    except OSError:
        durability_denied = True
    durability_completion = store.complete_agent_host_enrollment(
        bootstrap_code=durability_bootstrap["bootstrap_code"],
        hostname="adapter18-durability.test", platform="linux",
        public_key_fingerprint="sha256:" + "2" * 64,
        completion_recovery_secret="ahr-" + "d" * 43, project=PROJECT)
    recovery_future = enrollment_store.time.time() + 3600
    with patch.object(enrollment_store.time, "time", return_value=recovery_future):
        durability_recoveries = [store.complete_agent_host_enrollment(
            bootstrap_code=durability_bootstrap["bootstrap_code"],
            hostname="adapter18-durability.test", platform="linux",
            public_key_fingerprint="sha256:" + "2" * 64,
            completion_recovery_secret="ahr-" + "d" * 43, project=PROJECT)
            for _ in range(2)]
    durability_revoke = client.post(
        "/ixp/v1/agent-host-enrollments/revoke",
        headers={"Authorization": f"Bearer {durability_completion['host_token']}"},
        json={"project": PROJECT, "host_id": "host/adapter18-durability",
              "reason": "time_advanced_recovery_proof", "final_status": "revoked"},
    )
    durability_principal = store.get_principal_by_token(
        PROJECT, durability_completion["host_token"])
    ok(durability_denied and not durability_http_calls
       and durability_completion.get("host_token")
       and all(item.get("host_token") == durability_completion["host_token"]
               for item in durability_recoveries)
       and durability_principal.get("revoked_at") is not None
       and durability_revoke.status_code == 200
       and durability_revoke.json().get("revoked") is True,
       "time-advanced duplicate completion keeps one bearer usable through revoke")

    response_loss_bootstrap = begin("host/adapter18-response-loss")
    response_loss_paths = paths("response-loss")
    response_loss_calls = [0]

    def lose_enrollment_response(*args, **kwargs):
        response = http(*args, **kwargs)
        response_loss_calls[0] += 1
        if response_loss_calls[0] == 1:
            raise enrollment.EnrollmentError("simulated enrollment response loss")
        return response

    try:
        enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code=response_loss_bootstrap["bootstrap_code"],
            base_url="https://switchboard.test", project=PROJECT,
            owner_user_id="user-adapter18", target_platform="linux",
            paths=response_loss_paths, lanes=["ADAPTER"], http=lose_enrollment_response,
            service_runner=fake_service, local_auth_runner=fake_codex,
            codex_executable=str(TEST_CODEX), hostname="adapter18-response-loss.test",
            start_service=False,
        )
        response_lost = False
    except enrollment.EnrollmentError:
        response_lost = True
    response_pending_identity = json.loads(
        (response_loss_paths["config_root"] / "identity.json").read_text())
    response_pending_state = json.loads(
        (response_loss_paths["state_root"] / "state.json").read_text())
    response_recovered = enrollment.install_host(
        bundle_dir=bundle_020, public_key_path=public_path,
        bootstrap_code=response_loss_bootstrap["bootstrap_code"],
        base_url="https://switchboard.test", project=PROJECT,
        owner_user_id="user-adapter18", target_platform="linux",
        paths=response_loss_paths, lanes=["ADAPTER"], http=http,
        service_runner=fake_service, local_auth_runner=fake_codex,
        codex_executable=str(TEST_CODEX), hostname="adapter18-response-loss.test",
        start_service=False,
    )
    response_identity = json.loads(
        (response_loss_paths["config_root"] / "identity.json").read_text())
    ok(response_lost
       and response_pending_state["status"] == "enrollment_retry_required"
       and response_pending_identity["completion_recovery_secret"].startswith("ahr-")
       and response_recovered["completion_recovered"] is True
       and response_identity.get("host_token")
       and "completion_recovery_secret" not in response_identity,
       "lost enrollment response reuses durable pending material and recovers without a new bootstrap")
    recovery_secret = response_pending_identity["completion_recovery_secret"]
    recovery_args = {
        "bootstrap_code": response_loss_bootstrap["bootstrap_code"],
        "hostname": "adapter18-response-loss.test",
        "platform": "linux",
        "public_key_fingerprint": response_identity["public_key_fingerprint"],
        "completion_recovery_secret": recovery_secret,
        "project": PROJECT,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicate_recoveries = list(pool.map(
            lambda _: store.complete_agent_host_enrollment(**recovery_args), range(2)))
    duplicate_tokens = [item.get("host_token") for item in duplicate_recoveries]
    ok(all(item.get("error_code") == "bootstrap_code_consumed"
           for item in duplicate_recoveries)
       and not any(duplicate_tokens),
       "durable finalization acknowledgement retires bootstrap recovery")

    def prove_finalization_resume(boundary: str) -> bool:
        bootstrap = begin(f"host/adapter18-resume-{boundary}")
        resume_paths = paths(f"resume-{boundary}")
        completion_calls = [0]

        def counted_http(*args, **kwargs):
            completion_calls[0] += int(str(args[1]).endswith(
                "/ixp/v1/agent-host-enrollments/complete"))
            return http(*args, **kwargs)

        original_atomic = enrollment._atomic_json
        original_render = enrollment.render_service
        failed = [False]
        identity_path = resume_paths["config_root"] / "identity.json"
        config_path = resume_paths["config_root"] / "config.json"
        state_path = resume_paths["state_root"] / "state.json"

        def injected_atomic(path, value, mode):
            step = str((value or {}).get("finalization_step") or "")
            matches = (
                (boundary == "identity" and Path(path) == identity_path
                 and bool((value or {}).get("host_token")))
                or (boundary == "config" and Path(path) == config_path)
                or (boundary == "identity_state" and Path(path) == state_path
                    and step == "identity_written")
                or (boundary == "config_state" and Path(path) == state_path
                    and step == "config_written")
                or (boundary == "render_state" and Path(path) == state_path
                    and step == "service_rendered")
                or (boundary == "start_state" and Path(path) == state_path
                    and step == "service_started")
            )
            if matches and not failed[0]:
                failed[0] = True
                raise OSError(f"injected {boundary} finalization failure")
            return original_atomic(path, value, mode)

        def injected_render(*args, **kwargs):
            if boundary == "render" and not failed[0]:
                failed[0] = True
                raise OSError("injected render failure")
            return original_render(*args, **kwargs)

        def injected_service(command, **kwargs):
            if boundary == "start" and not failed[0]:
                failed[0] = True
                return subprocess.CompletedProcess(command, 1, "", "injected start failure")
            return fake_service(command, **kwargs)

        enrollment._atomic_json = injected_atomic
        enrollment.render_service = injected_render
        try:
            enrollment.install_host(
                bundle_dir=bundle_020, public_key_path=public_path,
                bootstrap_code=bootstrap["bootstrap_code"],
                base_url="https://switchboard.test", project=PROJECT,
                owner_user_id="user-adapter18", target_platform="linux",
                paths=resume_paths, lanes=["ADAPTER"], http=counted_http,
                service_runner=injected_service, local_auth_runner=fake_codex,
                codex_executable=str(TEST_CODEX), hostname=f"resume-{boundary}.test",
                start_service=boundary in {"start", "start_state"},
            )
            first_failed = False
        except (OSError, enrollment.EnrollmentError):
            first_failed = True
        finally:
            enrollment._atomic_json = original_atomic
            enrollment.render_service = original_render
        pending = json.loads(state_path.read_text())
        resumed = enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code=bootstrap["bootstrap_code"],
            base_url="https://switchboard.test", project=PROJECT,
            owner_user_id="user-adapter18", target_platform="linux",
            paths=resume_paths, lanes=["ADAPTER"], http=counted_http,
            service_runner=fake_service, local_auth_runner=fake_codex,
            codex_executable=str(TEST_CODEX), hostname=f"resume-{boundary}.test",
            start_service=boundary in {"start", "start_state"},
        )
        return bool(
            first_failed and failed[0]
            and pending.get("status") == "install_finalization_retry_required"
            and (pending.get("pending_identity") or {}).get("host_token")
            and resumed.get("installed")
            and completion_calls[0] == 1
        )

    resume_boundaries = (
        "identity", "identity_state", "config", "config_state",
        "render", "render_state", "start", "start_state",
    )
    ok(all(prove_finalization_resume(boundary) for boundary in resume_boundaries),
       "every post-completion write, render, and service-start boundary resumes without re-enrollment")

    revoke_partial_bootstrap = begin("host/adapter18-revoke-partial")
    revoke_partial_paths = paths("revoke-partial")
    original_atomic = enrollment._atomic_json
    partial_failed = [False]
    partial_identity_path = revoke_partial_paths["config_root"] / "identity.json"

    def fail_final_identity_once(path, value, mode):
        if (Path(path) == partial_identity_path and (value or {}).get("host_token")
                and not partial_failed[0]):
            partial_failed[0] = True
            raise OSError("injected partial revoke boundary")
        return original_atomic(path, value, mode)

    enrollment._atomic_json = fail_final_identity_once
    try:
        enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code=revoke_partial_bootstrap["bootstrap_code"],
            base_url="https://switchboard.test", project=PROJECT,
            owner_user_id="user-adapter18", target_platform="linux",
            paths=revoke_partial_paths, http=http, service_runner=fake_service,
            local_auth_runner=fake_codex, codex_executable=str(TEST_CODEX), start_service=False)
    except OSError:
        pass
    finally:
        enrollment._atomic_json = original_atomic
    partial_revoked = enrollment.revoke_host(
        identity_path=partial_identity_path,
        config_path=revoke_partial_paths["config_root"] / "config.json",
        state_path=revoke_partial_paths["state_root"] / "state.json",
        http=http, service_runner=fake_service)
    partial_record = store.get_agent_host_enrollment(
        "host/adapter18-revoke-partial", project=PROJECT)
    ok(partial_failed[0] and partial_revoked.get("revoked")
       and partial_record.get("status") == "revoked"
       and not partial_identity_path.exists(),
       "a post-completion install can be revoked directly from its durable finalization journal")

    mac_bootstrap = begin("host/adapter18-macos")
    mac_paths = paths("macos")
    mac_install = enrollment.install_host(
        bundle_dir=bundle_020,
        public_key_path=public_path,
        bootstrap_code=mac_bootstrap["bootstrap_code"],
        base_url="https://switchboard.test",
        project=PROJECT,
        owner_user_id="user-local-widened",
        target_platform="darwin",
        paths=mac_paths,
        lanes=["ADAPTER"],
        tenant_allowlist=["tenant-local-widened"],
        provider_allowlist=["unauthorized-provider"],
        http=http,
        service_runner=fake_service,
        local_auth_runner=fake_codex,
        codex_executable=str(TEST_CODEX),
        hostname="adapter18-mac.test",
    )
    mac_identity_path = mac_paths["config_root"] / "identity.json"
    mac_config_path = mac_paths["config_root"] / "config.json"
    mac_state_path = mac_paths["state_root"] / "state.json"
    mac_identity = json.loads(mac_identity_path.read_text())
    mac_config = json.loads(mac_config_path.read_text())
    initial_mac_token = mac_identity["host_token"]
    ok(mac_install["installed"] and stat.S_IMODE(mac_identity_path.stat().st_mode) == 0o600,
       "fresh macOS enrollment installs a 0600 rotatable identity")
    ok(mac_config["owner_user_id"] == "user-adapter18"
       and mac_config["tenant_allowlist"] == ["tenant-adapter18"]
       and mac_config["provider_allowlist"] == ["openai-codex"]
       and mac_config["lanes"] == ["ADAPTER"]
       and mac_config["capabilities"] == ["docs", "github", "python", "tests"]
       and mac_config["max_sessions"] == 1
       and mac_config["personal_wakes_only"] is True
       and mac_config["platform"] == "darwin"
       and mac_config["service_path"] == str(mac_paths["service_path"]),
       "installed policy comes only from the server-issued enrollment record")
    ok(codex_calls[:2] == [[str(TEST_CODEX), "--version"],
                           [str(TEST_CODEX), "login", "status"]]
       and mac_config["codex_executable"] == str(TEST_CODEX),
       "install proves the native Codex CLI and host-local ChatGPT login before bootstrap")
    ok(mac_paths["service_path"].is_file()
       and b"LaunchAgents" not in mac_paths["service_path"].read_bytes()
       and service_calls[-1][:2] == ["launchctl", "bootstrap"],
       "macOS install renders and starts a per-user launchd service")

    consumed = store.complete_agent_host_enrollment(
        bootstrap_code=mac_bootstrap["bootstrap_code"], hostname="replay",
        platform="darwin", public_key_fingerprint="sha256:" + "1" * 64,
        completion_recovery_secret="ahr-" + "x" * 43,
        project=PROJECT)
    ok(consumed.get("error_code") == "bootstrap_code_consumed",
       "device bootstrap is single-use")

    register = client.post("/ixp/v1/register_host", headers={
        "Authorization": f"Bearer {initial_mac_token}"}, json={
            "project": PROJECT,
            "host_id": "host/adapter18-macos",
            "agent_host_version": "0.2.0",
            "runtimes": [{
                "runtime": "codex",
                "lanes": ["ADAPTER"],
                "capabilities": ["docs", "github", "python", "tests"],
                "policy": {"allow_work": True, "allow_global_claim": False},
                "local_auth": {
                    "available": True, "runtime": "codex",
                    "auth_mode": "chatgpt_personal",
                    "account_fingerprint": "acct-test",
                    "credential_values_redacted": True,
                    "provider_credential_exported": False,
                },
            }],
            "limits": {"max_sessions": 1},
            "capacity": {
                "owner": {"user_id": "user-adapter18",
                    "tenant_allowlist": ["tenant-adapter18"],
                    "project_allowlist": [PROJECT],
                    "provider_allowlist": ["openai-codex"]},
                "local_auth": {
                    "available": True, "runtime": "codex",
                    "auth_mode": "chatgpt_personal",
                    "account_fingerprint": "acct-test",
                    "credential_values_redacted": True,
                    "provider_credential_exported": False,
                }},
        })
    ok(register.status_code == 200 and register.json().get("host_id") == "host/adapter18-macos",
       "enrolled principal registers only its exact host identity")
    host_principal = store.get_principal_by_token(PROJECT, initial_mac_token)
    victim_principal = store.create_principal(
        kind="host", display_name="host/runner-victim", token="runner-victim-token",
        scopes=["read", "write:ixp"], principal_id="host-runner-victim",
        project=PROJECT)
    store.upsert_runner_session({
        "runner_session_id": "run-owned-by-victim", "host_id": "host/runner-victim",
        "agent_id": "codex/runner-victim", "runtime": "codex", "status": "running",
    }, principal_id=victim_principal["id"], actor="host/runner-victim", project=PROJECT)
    runner_identity_hijack = client.post(
        "/ixp/v1/register_runner_session",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={"project": PROJECT, "runner_session_id": "run-owned-by-victim",
              "host_id": "host/adapter18-macos", "agent_id": "codex/attacker",
              "runtime": "codex", "status": "running"},
    )
    atomic_runner_hijack = store.upsert_runner_session({
        "runner_session_id": "run-owned-by-victim", "host_id": "host/adapter18-macos",
        "agent_id": "codex/attacker", "runtime": "codex", "status": "running",
    }, principal_id=host_principal["id"], actor="host/adapter18-macos", project=PROJECT)
    runner_after_hijack = store.get_runner_session("run-owned-by-victim", project=PROJECT)
    generic_wake_write = client.post(
        "/ixp/v1/request_wake",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={"project": PROJECT, "selector": {"runtime": "codex"},
              "reason": "must be denied", "task_id": "ADAPTER-18"},
    )
    generic_wake = store.request_wake(
        selector={"runtime": "codex", "lane": "ADAPTER"},
        reason="narrow host must not complete generic wake", source="test",
        actor="test", project=PROJECT)
    generic_wake_completion = client.post(
        "/txp/v1/complete_wake",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={"project": PROJECT, "wake_id": generic_wake["wake_id"],
              "runner_session_id": "run-generic", "agent_id": "codex/generic",
              "result": {"started": True}},
    )
    cross_host_runner = client.post(
        "/ixp/v1/register_runner_session",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={"project": PROJECT, "runner_session_id": "run-cross-host",
              "host_id": "host/not-owned", "agent_id": "codex/cross-host",
              "runtime": "codex", "status": "running"},
    )
    spoofed_host_registration = client.post(
        "/ixp/v1/register_host",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={"project": PROJECT, "host_id": "host/not-owned", "runtimes": []},
    )
    spoofed_host_heartbeat = client.post(
        "/ixp/v1/heartbeat_host",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={"project": PROJECT, "host_id": "host/not-owned", "status": "online"},
    )
    with (patch.object(store, "list_wake_intents", return_value=[{
            "wake_id": "wake-exact-only",
            "policy": {
                "require_exact_host_binding": True,
                "execution_binding": {"host_principal_id": host_principal["id"]},
            },
          }]),
          patch.object(complete_wake_command, "execute_mapping_result",
                       return_value={"status": "completed"})):
        exact_only_completion = client.post(
            "/txp/v1/complete_wake",
            headers={"Authorization": f"Bearer {initial_mac_token}"},
            json={"project": PROJECT, "wake_id": "wake-exact-only",
                  "runner_session_id": "run-exact-only",
                  "agent_id": "codex/exact-only", "result": {"started": True}},
        )
    exact_binding = {
        "task_id": "ADAPTER-18", "claim_id": "taskclaim-exact",
        "work_session_id": "worksession-exact", "runner_session_id": "run-exact",
        "host_id": "host/adapter18-macos", "agent_id": "codex/exact",
        "wake_id": "wake-exact", "source_sha": "a" * 40,
        "execution_connection_id": "execconn-exact", "completed_head_sha": "b" * 40,
    }
    with (patch.object(store, "check_personal_execution_authority",
                       return_value={"allowed": True}),
          patch.object(complete_claim_command, "execute_mapping_result",
                       return_value={"completed": True}),
          patch.object(store, "abandon_claim", return_value={"abandoned": True}),
          patch.object(work_session_commands, "update", return_value={"updated": True})):
        exact_claim_completion = client.post(
            "/txp/v1/complete_claim",
            headers={"Authorization": f"Bearer {initial_mac_token}"},
            json={"project": PROJECT, "claim_id": "taskclaim-exact", "evidence": "{}",
                  "personal_execution_binding": exact_binding},
        )
        wrong_claim_completion = client.post(
            "/txp/v1/complete_claim",
            headers={"Authorization": f"Bearer {initial_mac_token}"},
            json={"project": PROJECT, "claim_id": "taskclaim-other", "evidence": "{}",
                  "personal_execution_binding": exact_binding},
        )
        exact_claim_abandon = client.post(
            "/txp/v1/abandon_claim",
            headers={"Authorization": f"Bearer {initial_mac_token}"},
            json={"project": PROJECT, "claim_id": "taskclaim-exact", "reason": "failed",
                  "personal_execution_binding": exact_binding},
        )
        exact_session_checkpoint = client.patch(
            "/ixp/v1/work_sessions/worksession-exact",
            headers={"Authorization": f"Bearer {initial_mac_token}"},
            json={"project": PROJECT, "agent_id": "codex/exact", "head_sha": "b" * 40,
                  "dirty_status": "clean", "conflict_marker_count": 0,
                  "personal_execution_binding": exact_binding},
        )
        wrong_session_checkpoint = client.patch(
            "/ixp/v1/work_sessions/worksession-other",
            headers={"Authorization": f"Bearer {initial_mac_token}"},
            json={"project": PROJECT, "agent_id": "codex/exact", "head_sha": "b" * 40,
                  "dirty_status": "clean", "conflict_marker_count": 0,
                  "personal_execution_binding": exact_binding},
        )
    ok(host_principal.get("scopes") == ["read", "write:agent_host"]
       and generic_wake_write.status_code == 403
       and generic_wake_completion.status_code == 403
       and cross_host_runner.status_code == 403
       and spoofed_host_registration.status_code == 403
       and spoofed_host_heartbeat.status_code == 403
       and exact_only_completion.status_code == 200
       and runner_identity_hijack.status_code == 403
       and atomic_runner_hijack.get("error_code") == "runner_identity_mismatch"
       and runner_after_hijack.get("principal_id") == victim_principal["id"]
       and exact_claim_completion.status_code == 200
       and wrong_claim_completion.status_code == 403
       and exact_claim_abandon.status_code == 200
       and exact_session_checkpoint.status_code == 200
       and wrong_session_checkpoint.status_code == 403,
       "enrolled bearer is fenced to its host, runner identity, and exact terminal tuple")
    original_auth_mode = os.environ.get("PM_AUTH_MODE")
    os.environ["PM_AUTH_MODE"] = "required"
    try:
        anonymous_wakes = client.get(
            "/txp/v1/list_wake_intents", params={"project": PROJECT})
        authenticated_wakes = client.get(
            "/txp/v1/list_wake_intents", params={"project": PROJECT},
            headers={"Authorization": f"Bearer {initial_mac_token}"})
        anonymous_hosts = client.get(
            "/ixp/v1/agent_hosts", params={"project": PROJECT})
        authenticated_hosts = client.get(
            "/ixp/v1/agent_hosts", params={"project": PROJECT},
            headers={"Authorization": f"Bearer {initial_mac_token}"})
        anonymous_host_status = client.get(
            "/ixp/v1/host_status",
            params={"project": PROJECT, "host_id": "host/adapter18-macos"})
        authenticated_host_status = client.get(
            "/ixp/v1/host_status",
            params={"project": PROJECT, "host_id": "host/adapter18-macos"},
            headers={"Authorization": f"Bearer {initial_mac_token}"})
    finally:
        if original_auth_mode is None:
            os.environ.pop("PM_AUTH_MODE", None)
        else:
            os.environ["PM_AUTH_MODE"] = original_auth_mode
    ok(anonymous_wakes.status_code == 401 and authenticated_wakes.status_code == 200,
       "personal wake bindings cannot be enumerated without project read authority")
    ok(anonymous_hosts.status_code == 401 and authenticated_hosts.status_code == 200
       and anonymous_host_status.status_code == 401
       and authenticated_host_status.status_code == 200,
       "host inventory and status require project read authority")

    def lose_rotation_response(*args, **kwargs):
        http(*args, **kwargs)
        raise enrollment.EnrollmentError("simulated response loss")

    try:
        enrollment.rotate_identity(
            identity_path=mac_identity_path, config_path=mac_config_path,
            http=lose_rotation_response, service_runner=fake_service)
        rotation_response_lost = False
    except enrollment.EnrollmentError:
        rotation_response_lost = True
    identity_after_loss = json.loads(mac_identity_path.read_text())
    old_token_denied_elsewhere = client.post(
        "/ixp/v1/register_host",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={"project": PROJECT, "host_id": "host/adapter18-macos", "runtimes": []},
    )
    recovery_principal = store.get_agent_host_rotation_recovery_principal(
        token=initial_mac_token, host_id="host/adapter18-macos", project=PROJECT)
    ok(rotation_response_lost
       and identity_after_loss["host_token"] == initial_mac_token
       and old_token_denied_elsewhere.status_code == 401
       and recovery_principal and recovery_principal.get("kind") == "host",
       "lost rotation response leaves local identity unchanged and old bearer denied elsewhere")
    rotated = enrollment.rotate_identity(
        identity_path=mac_identity_path, config_path=mac_config_path, http=http,
        service_runner=fake_service)
    mac_identity = json.loads(mac_identity_path.read_text())
    rotated_token = mac_identity["host_token"]
    ok(rotated["identity_generation"] == 3 and rotated_token != initial_mac_token
       and rotated["service_restarted"] is True
       and service_calls[-1][:3] == ["launchctl", "kickstart", "-k"],
       "bounded rotation recovery persists the bearer and restarts the live daemon")
    ok(store.get_principal_by_token(PROJECT, initial_mac_token) is None
       and store.get_principal_by_token(PROJECT, rotated_token) is not None,
       "rotated bearer invalidates the previous token immediately")

    manifest_021 = enrollment.create_signed_bundle(ROOT, bundle_021, "0.2.1", private_path)
    updated = enrollment.update_host(
        bundle_dir=bundle_021, public_key_path=public_path, state_path=mac_state_path,
        service_runner=fake_service)
    retry_prefix = TMP / "signed-release-retry"
    retry_release = enrollment._install_release(bundle_021, manifest_021, retry_prefix)
    retry_entrypoint = retry_release / manifest_021["entrypoint"]
    retry_entrypoint.write_text("locally corrupted release", encoding="utf-8")
    try:
        enrollment._install_release(bundle_021, manifest_021, retry_prefix)
        corrupted_retry_denied = False
    except enrollment.EnrollmentError:
        corrupted_retry_denied = True
    ok(updated == {"updated": True, "version": "0.2.1"}
       and (mac_paths["prefix"] / "current").resolve().name == "0.2.1"
       and json.loads(mac_config_path.read_text())["agent_host_version"] == "0.2.1"
       and corrupted_retry_denied,
       "signed update advances current/config and refuses mismatched pre-existing release bytes")
    try:
        enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code="ahb-rejected-install-must-not-switch-current",
            base_url="https://switchboard.test", project=PROJECT,
            owner_user_id="user-adapter18", target_platform="darwin",
            paths=mac_paths, http=http, service_runner=fake_service,
            local_auth_runner=fake_codex, codex_executable=str(TEST_CODEX),
            start_service=False)
        mismatched_reinstall_denied = False
    except enrollment.EnrollmentError:
        mismatched_reinstall_denied = True
    ok(mismatched_reinstall_denied
       and (mac_paths["prefix"] / "current").resolve().name == "0.2.1",
       "a rejected existing-state install cannot change the selected release")

    def offline(*args, **kwargs):
        del args, kwargs
        raise enrollment.EnrollmentError("offline")

    try:
        enrollment.revoke_host(
            identity_path=mac_identity_path, config_path=mac_config_path,
            state_path=mac_state_path, http=offline, service_runner=fake_service)
        offline_visible = False
    except enrollment.EnrollmentError:
        offline_visible = True
    pending_state = json.loads(mac_state_path.read_text())
    ok(offline_visible and pending_state["status"] == "revocation_response_unknown"
       and mac_identity_path.is_file(),
       "offline revoke stops work but preserves retry identity as visible pending state")

    def lose_revoke_response(*args, **kwargs):
        response = http(*args, **kwargs)
        raise enrollment.EnrollmentError("simulated committed revoke response loss")

    try:
        enrollment.revoke_host(
            identity_path=mac_identity_path, config_path=mac_config_path,
            state_path=mac_state_path, http=lose_revoke_response,
            service_runner=fake_service)
        revoke_response_lost = False
    except enrollment.EnrollmentError:
        revoke_response_lost = True
    committed_unknown = json.loads(mac_state_path.read_text())
    old_revoked_token_denied = client.post(
        "/ixp/v1/register_host",
        headers={"Authorization": f"Bearer {rotated_token}"},
        json={"project": PROJECT, "host_id": "host/adapter18-macos", "runtimes": []},
    )

    revoked = enrollment.revoke_host(
        identity_path=mac_identity_path, config_path=mac_config_path,
        state_path=mac_state_path, http=http, service_runner=fake_service)
    mac_record = store.get_agent_host_enrollment("host/adapter18-macos", project=PROJECT)
    ok(revoke_response_lost
       and committed_unknown["status"] == "revocation_response_unknown"
       and old_revoked_token_denied.status_code == 401
       and revoked["revoked"] and mac_record["status"] == "revoked"
       and not mac_identity_path.exists() and not mac_paths["service_path"].exists(),
       "committed revoke resumes, purges locally, and persistently disables the LaunchAgent")
    denied = store.register_host(
        {"host_id": "host/adapter18-macos", "runtimes": []},
        principal_id=mac_record["principal_id"], actor="test", project=PROJECT)
    ok(denied.get("error_code") == "host_identity_revoked",
       "revoked host is denied even if a caller tries to reuse its host id")
    residue = enrollment.residue_scan([
        mac_paths["config_root"], mac_paths["state_root"] / "provider-runtimes"])
    ok(residue["residue_free"], "post-revoke secret-residue scan is clean")

    linux_bootstrap = begin("host/adapter18-linux")
    linux_paths = paths("linux")
    linux_install = enrollment.install_host(
        bundle_dir=bundle_020,
        public_key_path=public_path,
        bootstrap_code=linux_bootstrap["bootstrap_code"],
        base_url="https://switchboard.test",
        project=PROJECT,
        owner_user_id="user-adapter18",
        target_platform="linux",
        paths=linux_paths,
        lanes=["ADAPTER"],
        http=http,
        service_runner=fake_service,
        local_auth_runner=fake_codex,
        codex_executable=str(TEST_CODEX),
        hostname="adapter18-linux.test",
    )
    linux_identity = linux_paths["config_root"] / "identity.json"
    linux_config = linux_paths["config_root"] / "config.json"
    linux_state = linux_paths["state_root"] / "state.json"
    linux_workspace_root = linux_paths["state_root"] / "workspaces"
    service_text = linux_paths["service_path"].read_text()
    ok(linux_install["installed"] and "NoNewPrivileges=yes" in service_text
       and str(linux_paths["state_root"]) in service_text
       and str(linux_workspace_root) in service_text
       and linux_workspace_root.is_dir()
       and stat.S_IMODE(linux_workspace_root.stat().st_mode) == 0o700
       and service_calls[-1][:3] == ["systemctl", "--user", "enable"],
       "Linux systemd service grants writes only to its protected state and workspace roots")

    captured_exec: dict[str, object] = {}
    original_execve = enrollment.os.execve
    original_openai_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "must-not-cross-agent-host-boundary"
    try:
        def capture_exec(executable, arguments, environment):
            captured_exec.update({
                "executable": executable,
                "arguments": arguments,
                "environment": environment,
            })
            raise RuntimeError("captured")

        enrollment.os.execve = capture_exec
        try:
            enrollment.service_run(linux_identity, linux_config)
        except RuntimeError as exc:
            ok(str(exc) == "captured", "service-run reaches the supervised native daemon")
    finally:
        enrollment.os.execve = original_execve
        if original_openai_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = original_openai_key
    launched_env = captured_exec.get("environment") or {}
    ok("OPENAI_API_KEY" not in launched_env
       and launched_env.get("PM_MCP_TOKEN")
       and launched_env.get("PM_AGENT_WORK_MODULE") == "adapters.codex_local_worker:run"
       and launched_env.get("PM_CODEX_EXECUTABLE") == str(TEST_CODEX)
       and launched_env.get("PM_PERSONAL_AGENT_HOST_EXECUTION") == "1"
       and launched_env.get("PM_PERSONAL_WORKSPACE_ROOT") == str(linux_workspace_root)
       and all(isinstance(value, str) for value in launched_env.values()),
       "service-run strips metered keys and binds the personal host to its writable root")

    binding_source = TMP / "personal-binding-source"
    binding_remote = TMP / "personal-binding-remote.git"
    binding_source.mkdir()
    subprocess.run(["git", "init", "-b", "master", str(binding_source)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(binding_source), "config", "user.email",
                    "adapter18@example.test"], check=True)
    subprocess.run(["git", "-C", str(binding_source), "config", "user.name",
                    "ADAPTER-18 Test"], check=True)
    (binding_source / "proof.txt").write_text("canonical source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(binding_source), "add", "proof.txt"], check=True)
    subprocess.run(["git", "-C", str(binding_source), "commit", "-m", "source"],
                   check=True, capture_output=True)
    binding_sha = subprocess.run(
        ["git", "-C", str(binding_source), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "clone", "--bare", str(binding_source), str(binding_remote)],
                   check=True, capture_output=True)
    coordinator_workspace = TMP / "coordination-vm-only" / "bound-session"
    binding_agent = "codex/ADAPTER-18-local-worker"
    binding_claim = "taskclaim-local-worker"
    binding_session = "worksession-local-worker"
    binding_task = {
        "task_id": "ADAPTER-18",
        "active_claims": [{"claim_id": binding_claim, "agent_id": binding_agent}],
    }
    binding_work_session = {
        "task_id": "ADAPTER-18", "agent_id": binding_agent,
        "claim_id": binding_claim, "work_session_id": binding_session,
        "status": "active", "head_sha": binding_sha,
        "branch": "codex/ADAPTER-18-local-worker", "policy_profile": "code_strict",
        "repo": binding_remote.as_uri(),
        "worktree_path": str(coordinator_workspace),
    }
    binding_environment = {
        "PM_TASK_ID": "ADAPTER-18",
        "PM_PERSONAL_AGENT_HOST_EXECUTION": "1",
        "PM_PERSONAL_WORKSPACE_ROOT": str(linux_workspace_root),
        "PM_AGENT_HOST_ALLOW_FILE_REPO": "1",
        "PM_SOURCE_SHA": binding_sha,
        "PM_CO_ACCOUNT_BINDING_JSON": json.dumps({
            "task_id": "ADAPTER-18", "claim_id": binding_claim,
            "work_session_id": binding_session,
        }),
    }
    with (patch.dict(os.environ, binding_environment),
          patch.object(switchboard_core, "get_task", return_value=binding_task),
          patch.object(switchboard_core, "get_work_session",
                       return_value=binding_work_session)):
        admitted_claim, admitted_context = switchboard_core._acquire_claim(
            PROJECT, binding_agent, ["ADAPTER"], "https://switchboard.test", "token",
            600, False, str(coordinator_workspace))
        materialized_workspace = Path(admitted_context["workspace_path"])
        (materialized_workspace / "untracked-attack.txt").write_text(
            "must be rejected\n", encoding="utf-8")
        denied_claim, denied_context = switchboard_core._acquire_claim(
            PROJECT, binding_agent, ["ADAPTER"], "https://switchboard.test", "token",
            600, False, str(coordinator_workspace))
    materialized_head = subprocess.run(
        ["git", "-C", str(materialized_workspace), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True).stdout.strip()
    ok(admitted_claim.get("claimed") is True and admitted_context.get("bound_existing") is True
       and materialized_workspace.resolve().is_relative_to(linux_workspace_root.resolve())
       and materialized_workspace != coordinator_workspace
       and materialized_head == binding_sha,
       "personal execution materializes the exact canonical SHA below the host-only workspace root")
    ok(denied_claim.get("claimed") is False and denied_context is None
       and "personal workspace is dirty" in denied_claim.get("reason", ""),
       "personal execution refuses reuse of a dirty host-local checkout")

    local_workspace = TMP / "local-worker"
    local_workspace.mkdir()
    source_sha = "a" * 40
    completed_sha = "b" * 40
    local_task = {
        "task_id": "ADAPTER-18",
        "title": "Local native worker test",
        "description": "Prove host-local Codex execution without a credential lease.",
        "claim_id": "taskclaim-local-worker",
        "managed": {
            "workspace_path": str(local_workspace),
            "work_session_id": "worksession-local-worker",
        },
    }
    try:
        codex_personal_worker._lease_body({}, local_task)
        central_binding_required = False
    except RuntimeError as exc:
        central_binding_required = str(exc) == "CO runtime binding is incomplete"
    original_local_git = codex_local_worker._git
    original_binding_env = {key: os.environ.get(key) for key in (
        "PM_CO_HOST_ID", "PM_RUNNER_SESSION_ID", "PM_CO_WAKE_ID", "PM_SOURCE_SHA",
        "PM_EXECUTION_CONNECTION_ID", "PM_AGENT_ID", "PM_CLAIM_ID",
        "PM_WORK_SESSION_ID", "PM_CO_ACCOUNT_BINDING_JSON", "PM_CODEX_EXECUTABLE",
        "OPENAI_API_KEY",
    )}
    local_git_heads = [source_sha, completed_sha]
    local_git_common = (TMP / "local-worker-git-common").resolve()
    local_git_dir = (local_git_common / "worktrees" / "local-worker").resolve()
    captured_local_codex: dict[str, object] = {}
    local_control_calls: list[tuple[str, dict]] = []
    local_completion_response_lost = [False]

    def fake_local_git(workspace, *args):
        ok(workspace == str(local_workspace), "native local worker stays in its managed workspace")
        if args == ("rev-parse", "HEAD"):
            return local_git_heads.pop(0)
        if args == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
            return str(local_git_common)
        if args == ("branch", "--show-current"):
            return "codex/ADAPTER-18-local-worker"
        if args == ("status", "--porcelain"):
            return ""
        if args == ("rev-parse", "@{upstream}"):
            return completed_sha
        raise AssertionError(args)

    def fake_local_codex(command, **kwargs):
        captured_local_codex.update({"command": command, "kwargs": kwargs})
        return subprocess.CompletedProcess(command, 0, "native codex completed", "")

    def fake_local_control(method, path, body):
        ok(method == "POST", "native local worker uses authenticated state-changing calls")
        local_control_calls.append((path, dict(body)))
        if path == "/txp/v1/complete_wake":
            if not local_completion_response_lost[0]:
                local_completion_response_lost[0] = True
                raise RuntimeError("simulated committed completion response loss")
            return {"status": "completed" if body["result"]["started"] else "failed"}
        return {"runner_session_id": body["runner_session_id"], "status": body["status"]}

    failed_local_control_calls: list[tuple[str, dict]] = []

    def fake_failed_local_control(method, path, body):
        ok(method == "POST", "failed native local worker uses authenticated writes")
        failed_local_control_calls.append((path, dict(body)))
        if path == "/txp/v1/complete_wake":
            return {"status": "failed"}
        return {"runner_session_id": body["runner_session_id"], "status": body["status"]}

    def fake_failed_local_codex(command, **kwargs):
        del command, kwargs
        return subprocess.CompletedProcess([], 1, "", "native failure")

    try:
        codex_local_worker._git = fake_local_git
        os.environ.update({
            "PM_CO_HOST_ID": "host/adapter18-linux",
            "PM_RUNNER_SESSION_ID": "runner-local-worker",
            "PM_CO_WAKE_ID": "wake-local-worker",
            "PM_SOURCE_SHA": source_sha,
            "PM_EXECUTION_CONNECTION_ID": "execconn-local-worker",
            "PM_AGENT_ID": "codex/ADAPTER-18-local-worker",
            "PM_CLAIM_ID": "taskclaim-local-worker",
            "PM_WORK_SESSION_ID": "worksession-local-worker",
            "PM_CODEX_EXECUTABLE": str(TEST_CODEX),
            "PM_CO_ACCOUNT_BINDING_JSON": json.dumps({
                "task_id": "ADAPTER-18",
                "claim_id": "taskclaim-local-worker",
                "work_session_id": "worksession-local-worker",
                "host_id": "host/adapter18-linux",
                "runner_session_id": "runner-local-worker",
                "agent_id": "codex/ADAPTER-18-local-worker",
            }),
            "OPENAI_API_KEY": "must-not-cross-local-worker-boundary",
        })
        local_evidence = codex_local_worker.run(
            local_task, runner=fake_local_codex,
            http=fake_local_control)
        local_git_heads[:] = [source_sha]
        try:
            codex_local_worker.run(
                local_task, runner=fake_failed_local_codex,
                http=fake_failed_local_control)
            failed_local_visible = False
        except RuntimeError as exc:
            failed_local_visible = "native Codex execution failed" in str(exc)
    finally:
        codex_local_worker._git = original_local_git
        for key, value in original_binding_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    local_command = captured_local_codex.get("command") or []
    local_environment = (captured_local_codex.get("kwargs") or {}).get("env") or {}
    ok(central_binding_required
       and local_evidence["head_sha"] == completed_sha
       and local_evidence["verification"]["auth_mode"] == "chatgpt_personal"
       and local_evidence["verification"]["provider_credential_exported"] is False
       and "exec" in local_command and "ADAPTER-18" in str(local_command[-1])
       and "workspace-write" in local_command
       and "danger-full-access" not in local_command
       and local_command.count("--add-dir") == 1
       and str(local_git_common) in local_command and str(local_git_dir) not in local_command
       and "OPENAI_API_KEY" not in local_environment
       and any(path == "/ixp/v1/heartbeat_runner_session"
               for path, _body in local_control_calls)
       and any(path == "/txp/v1/complete_wake"
               and body["result"]["started"] is True
               for path, body in local_control_calls)
       and len([body for path, body in local_control_calls
                if path == "/txp/v1/complete_wake"]) == 2
       and len({json.dumps(body, sort_keys=True) for path, body in local_control_calls
                if path == "/txp/v1/complete_wake"}) == 1
       and local_control_calls[-1][1]["status"] == "completed",
       "native local worker heartbeats and exactly retries/terminalizes its wake and runner")
    failed_runner_index = next(
        index for index, (path, body) in enumerate(failed_local_control_calls)
        if path == "/ixp/v1/register_runner_session" and body.get("status") == "failed")
    failed_wake_index = next(
        index for index, (path, _body) in enumerate(failed_local_control_calls)
        if path == "/txp/v1/complete_wake")
    ok(failed_local_visible and failed_runner_index < failed_wake_index
       and failed_local_control_calls[failed_wake_index][1]["result"]["started"] is False,
       "native execution failure terminalizes its exact runner before the failed wake receipt")
    linux_runtime_root = Path(json.loads(linux_config.read_text())["runtime_root"])
    linux_runtime_root.mkdir(parents=True, exist_ok=True)
    (linux_runtime_root / "residue.txt").write_text("non-secret runtime residue")
    original_atomic = enrollment._atomic_json
    original_rmtree = enrollment.shutil.rmtree
    cleanup_failures = []

    def fail_uninstall_boundary(boundary):
        failed_once = [False]

        def injected_atomic(path, value, mode):
            step = str((value or {}).get("cleanup_step") or "")
            target = {
                "identity_state": "identity_deleted",
                "runtime_state": "runtime_residue_deleted",
                "service_state": "service_deleted",
                "releases_state": "releases_deleted",
                "config_state": "config_deleted",
            }.get(boundary)
            if target == step and not failed_once[0]:
                failed_once[0] = True
                raise OSError(f"injected uninstall {boundary}")
            return original_atomic(path, value, mode)

        def injected_rmtree(path, *args, **kwargs):
            target = {
                "runtime_delete": linux_runtime_root,
                "prefix_delete": linux_paths["prefix"],
                "config_delete": linux_paths["config_root"],
                "state_delete": linux_paths["state_root"],
            }.get(boundary)
            if target is not None and Path(path) == target and not failed_once[0]:
                failed_once[0] = True
                raise OSError(f"injected uninstall {boundary}")
            return original_rmtree(path, *args, **kwargs)

        enrollment._atomic_json = injected_atomic
        enrollment.shutil.rmtree = injected_rmtree
        try:
            enrollment.uninstall_host(
                identity_path=linux_identity, config_path=linux_config,
                state_path=linux_state, http=http, service_runner=fake_service)
        except OSError:
            cleanup_failures.append(boundary)
        finally:
            enrollment._atomic_json = original_atomic
            enrollment.shutil.rmtree = original_rmtree

    cleanup_boundaries = (
        "identity_state", "runtime_delete", "runtime_state", "service_state",
        "prefix_delete", "releases_state", "config_delete", "config_state",
        "state_delete",
    )
    for cleanup_boundary in cleanup_boundaries:
        fail_uninstall_boundary(cleanup_boundary)

    uninstalled = enrollment.uninstall_host(
        identity_path=linux_identity, config_path=linux_config, state_path=linux_state,
        http=http, service_runner=fake_service)
    linux_record = store.get_agent_host_enrollment("host/adapter18-linux", project=PROJECT)
    ok(cleanup_failures == list(cleanup_boundaries)
       and uninstalled["uninstalled"] and linux_record["status"] == "uninstalled"
       and not linux_paths["prefix"].exists()
       and not linux_paths["service_path"].exists(),
       "Linux uninstall resumes after every local deletion and journal boundary")

    public_records = json.dumps(store.list_agent_host_enrollments(project=PROJECT), sort_keys=True)
    ok("bootstrap_hash" not in public_records and "host_token" not in public_records
       and initial_mac_token not in public_records and rotated_token not in public_records,
       "Switchboard enrollment readback never exposes bootstrap or host credentials")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
