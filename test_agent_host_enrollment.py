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


TMP = Path(tempfile.mkdtemp(prefix="adapter18-agent-host-enrollment-"))
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
from adapters import agent_host_enrollment as enrollment  # noqa: E402
from adapters import codex_local_worker, codex_personal_worker  # noqa: E402
from app import app  # noqa: E402


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
    try:
        enrollment.preflight_codex_local_auth(
            codex_executable="codex", runner=fake_api_key_codex)
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
            codex_executable="codex",
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
    ok(durability_denied and not durability_http_calls
       and durability_completion.get("host_token"),
       "release and 0600 identity storage must be durable before bootstrap consumption")

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
            codex_executable="codex", hostname="adapter18-response-loss.test",
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
        codex_executable="codex", hostname="adapter18-response-loss.test",
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
    ok(all(item.get("completed") for item in duplicate_recoveries)
       and len(set(duplicate_tokens)) == 1
       and duplicate_tokens[0] == response_identity["host_token"]
       and store.get_principal_by_token(PROJECT, duplicate_tokens[0]) is not None,
       "concurrent completion retries converge on one still-usable recovered bearer")

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
                codex_executable="codex", hostname=f"resume-{boundary}.test",
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
            codex_executable="codex", hostname=f"resume-{boundary}.test",
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
            local_auth_runner=fake_codex, codex_executable="codex", start_service=False)
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
        owner_user_id="user-adapter18",
        target_platform="darwin",
        paths=mac_paths,
        lanes=["ADAPTER"],
        tenant_allowlist=["tenant-adapter18"],
        http=http,
        service_runner=fake_service,
        local_auth_runner=fake_codex,
        codex_executable="codex",
        hostname="adapter18-mac.test",
    )
    mac_identity_path = mac_paths["config_root"] / "identity.json"
    mac_config_path = mac_paths["config_root"] / "config.json"
    mac_state_path = mac_paths["state_root"] / "state.json"
    mac_identity = json.loads(mac_identity_path.read_text())
    initial_mac_token = mac_identity["host_token"]
    ok(mac_install["installed"] and stat.S_IMODE(mac_identity_path.stat().st_mode) == 0o600,
       "fresh macOS enrollment installs a 0600 rotatable identity")
    ok(codex_calls[:2] == [["codex", "--version"], ["codex", "login", "status"]],
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
            "runtimes": [{"runtime": "codex", "lanes": ["ADAPTER"]}],
            "limits": {"max_sessions": 1},
            "capacity": {"local_auth": {"available": True,
                "credential_values_redacted": True}},
        })
    ok(register.status_code == 200 and register.json().get("host_id") == "host/adapter18-macos",
       "enrolled principal registers only its exact host identity")

    def lose_rotation_response(*args, **kwargs):
        http(*args, **kwargs)
        raise enrollment.EnrollmentError("simulated response loss")

    try:
        enrollment.rotate_identity(
            identity_path=mac_identity_path, config_path=mac_config_path,
            http=lose_rotation_response)
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
        identity_path=mac_identity_path, config_path=mac_config_path, http=http)
    mac_identity = json.loads(mac_identity_path.read_text())
    rotated_token = mac_identity["host_token"]
    ok(rotated["identity_generation"] == 3 and rotated_token != initial_mac_token,
       "bounded rotation-only recovery retries after response loss and persists atomically")
    ok(store.get_principal_by_token(PROJECT, initial_mac_token) is None
       and store.get_principal_by_token(PROJECT, rotated_token) is not None,
       "rotated bearer invalidates the previous token immediately")

    enrollment.create_signed_bundle(ROOT, bundle_021, "0.2.1", private_path)
    updated = enrollment.update_host(
        bundle_dir=bundle_021, public_key_path=public_path, state_path=mac_state_path,
        service_runner=fake_service)
    ok(updated == {"updated": True, "version": "0.2.1"}
       and (mac_paths["prefix"] / "current").resolve().name == "0.2.1",
       "signed update advances the atomic current release and restarts launchd")

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
    ok(offline_visible and pending_state["status"] == "revocation_pending"
       and mac_identity_path.is_file(),
       "offline revoke stops work but preserves retry identity as visible pending state")

    revoked = enrollment.revoke_host(
        identity_path=mac_identity_path, config_path=mac_config_path,
        state_path=mac_state_path, http=http, service_runner=fake_service)
    mac_record = store.get_agent_host_enrollment("host/adapter18-macos", project=PROJECT)
    ok(revoked["revoked"] and mac_record["status"] == "revoked"
       and not mac_identity_path.exists(),
       "successful revoke fences the server identity and purges the local bearer")
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
        codex_executable="codex",
        hostname="adapter18-linux.test",
    )
    linux_identity = linux_paths["config_root"] / "identity.json"
    linux_config = linux_paths["config_root"] / "config.json"
    linux_state = linux_paths["state_root"] / "state.json"
    service_text = linux_paths["service_path"].read_text()
    ok(linux_install["installed"] and "NoNewPrivileges=yes" in service_text
       and str(linux_paths["state_root"]) in service_text
       and service_calls[-1][:3] == ["systemctl", "--user", "enable"],
       "Linux systemd service is hardened but can write its owned state roots")

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
       and launched_env.get("PM_AGENT_WORK_MODULE") == "adapters.codex_local_worker:run",
       "service-run strips metered keys and binds only the narrow host identity")

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
        "PM_WORK_SESSION_ID", "PM_CO_ACCOUNT_BINDING_JSON", "OPENAI_API_KEY",
    )}
    local_git_heads = [source_sha, completed_sha]
    captured_local_codex: dict[str, object] = {}

    def fake_local_git(workspace, *args):
        ok(workspace == str(local_workspace), "native local worker stays in its managed workspace")
        if args == ("rev-parse", "HEAD"):
            return local_git_heads.pop(0)
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
            local_task, runner=fake_local_codex, codex_executable="codex")
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
       and "OPENAI_API_KEY" not in local_environment,
       "fresh enrollment selects a native local-auth worker with no central credential binding")
    uninstalled = enrollment.uninstall_host(
        identity_path=linux_identity, config_path=linux_config, state_path=linux_state,
        http=http, service_runner=fake_service)
    linux_record = store.get_agent_host_enrollment("host/adapter18-linux", project=PROJECT)
    ok(uninstalled["uninstalled"] and linux_record["status"] == "uninstalled"
       and not linux_paths["prefix"].exists()
       and not linux_paths["service_path"].exists(),
       "Linux uninstall revokes remotely and removes service, releases, config, and state")

    public_records = json.dumps(store.list_agent_host_enrollments(project=PROJECT), sort_keys=True)
    ok("bootstrap_hash" not in public_records and "host_token" not in public_records
       and initial_mac_token not in public_records and rotated_token not in public_records,
       "Switchboard enrollment readback never exposes bootstrap or host credentials")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
