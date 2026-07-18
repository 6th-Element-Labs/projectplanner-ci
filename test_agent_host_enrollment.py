#!/usr/bin/env python3
"""ADAPTER-18: signed macOS/Linux enrollment, rotation, revoke, and residue proof."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import base64
import inspect
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from unittest.mock import patch


TMP = Path(tempfile.mkdtemp(prefix="adapter18-agent-host-enrollment-"))
TEST_CODEX = TMP / "bin" / "codex"
TEST_CODEX.parent.mkdir()
TEST_CODEX.write_text(
    "#!/bin/sh\n"
    "if [ \"$1\" = \"--version\" ]; then echo 'codex-cli 1.2.3'; exit 0; fi\n"
    "if [ \"$1\" = \"login\" ] && [ \"$2\" = \"status\" ]; then "
    "echo 'Logged in using ChatGPT'; exit 0; fi\n"
    "exit 1\n",
    encoding="utf-8",
)
TEST_CODEX.chmod(0o755)
TEST_CODEX = TEST_CODEX.resolve()
TEST_USER_CODEX_HOME = TMP / "user-codex-home"
TEST_USER_CODEX_HOME.mkdir(mode=0o700)
(TEST_USER_CODEX_HOME / "auth.json").write_text(
    json.dumps({"tokens": {"access_token": "user-login-seed"}}), encoding="utf-8")
os.chmod(TEST_USER_CODEX_HOME / "auth.json", 0o600)
os.environ["CODEX_HOME"] = str(TEST_USER_CODEX_HOME)
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"U" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "ui21-host-test:v1"

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

import store  # noqa: E402
from switchboard.storage.repositories import agent_host_enrollments as enrollment_store  # noqa: E402
from switchboard.storage.repositories import coordination as coordination_store  # noqa: E402
from switchboard.storage.repositories.provider_credentials import (  # noqa: E402
    default_provider_credential_repository as credential_repository,
)
from adapters import agent_host, agent_host_enrollment as enrollment  # noqa: E402
from adapters import codex_local_worker, codex_personal_worker, switchboard_core  # noqa: E402
from app import app  # noqa: E402
from switchboard.application.commands import complete_claim as complete_claim_command  # noqa: E402
from switchboard.application.commands import complete_wake as complete_wake_command  # noqa: E402
from switchboard.application.commands import agent_host_enrollment as enrollment_command  # noqa: E402
from switchboard.application.commands import work_sessions as work_session_commands  # noqa: E402
from switchboard.application.contracts.agents import (  # noqa: E402
    BeginHostEnrollmentCommand,
    CompleteHostEnrollmentCommand,
    FinalizeHostEnrollmentCommand,
    RevokeHostIdentityCommand,
    RotateHostIdentityCommand,
)
from switchboard.mcp.tools import agents as agent_tools  # noqa: E402


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
codex_call_homes: list[str] = []


def fake_service(command, **kwargs):
    del kwargs
    service_calls.append(list(command))
    return subprocess.CompletedProcess(command, 0, "", "")


def fake_codex(command, **kwargs):
    environment = kwargs.get("env") or {}
    codex_calls.append(list(command))
    codex_call_homes.append(str(environment.get("CODEX_HOME") or ""))
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
        "lane_allowlist": ["ADAPTER"],
        "package_version": "0.2.0",
        "ttl_seconds": 300,
    })
    ok(response.status_code == 200 and response.json().get("created") is True,
       f"Switchboard issues one short-lived bootstrap for {host_id}")
    return response.json()


try:
    store.init_db(PROJECT)
    missing_project_requests = [
        ("/ixp/v1/agent-host-enrollments", {
            "owner_user_id": "user-adapter18",
        }),
        ("/ixp/v1/agent-host-enrollments/complete", {
            "bootstrap_code": "ahb-test", "hostname": "missing-project",
            "platform": "linux", "public_key_fingerprint": "sha256:" + "1" * 64,
            "completion_recovery_secret": "ahr-" + "x" * 43,
        }),
        ("/ixp/v1/agent-host-enrollments/finalize", {
            "enrollment_id": "enrollment-missing-project",
            "host_id": "host/missing-project",
        }),
        ("/ixp/v1/agent-host-enrollments/rotate", {
            "host_id": "host/missing-project",
            "public_key_fingerprint": "sha256:" + "2" * 64,
        }),
        ("/ixp/v1/agent-host-enrollments/revoke", {
            "host_id": "host/missing-project",
        }),
    ]
    missing_project_responses = [
        client.post(path, json=payload)
        for path, payload in missing_project_requests
    ]
    ok(all(response.status_code == 422 for response in missing_project_responses),
       "every enrollment lifecycle ingress requires an explicit project")
    blank_project_responses = [
        client.post(path, json={**payload, "project": "   "})
        for path, payload in missing_project_requests
    ]
    ok(all(response.status_code == 422 for response in blank_project_responses),
       "every enrollment lifecycle ingress rejects a blank project")

    blank_contract_cases = [
        (BeginHostEnrollmentCommand, {
            "project": " ", "owner_user_id": "user-adapter18",
        }),
        (CompleteHostEnrollmentCommand, {
            "project": " ", "bootstrap_code": "ahb-test", "hostname": "host",
            "platform": "linux", "public_key_fingerprint": "sha256:" + "1" * 64,
            "completion_recovery_secret": "ahr-" + "x" * 43,
        }),
        (FinalizeHostEnrollmentCommand, {
            "project": " ", "enrollment_id": "enrollment-test", "host_id": "host/test",
        }),
        (RotateHostIdentityCommand, {
            "project": " ", "host_id": "host/test",
            "public_key_fingerprint": "sha256:" + "2" * 64,
        }),
        (RevokeHostIdentityCommand, {
            "project": " ", "host_id": "host/test",
        }),
    ]
    blank_contract_rejected = []
    for contract, payload in blank_contract_cases:
        try:
            contract.model_validate(payload)
            blank_contract_rejected.append(False)
        except Exception:
            blank_contract_rejected.append(True)
    checked_in_schemas = [
        json.loads((ROOT / "schemas" / f"{contract.SCHEMA}.json").read_text())
        for contract, _payload in blank_contract_cases
    ]

    checked_in_instance_results = []
    for schema, (_contract, payload) in zip(checked_in_schemas, blank_contract_cases):
        validator = Draft202012Validator(schema)
        valid = {**payload, "project": PROJECT}
        omitted = dict(valid)
        omitted.pop("project")
        checked_in_instance_results.append(
            not list(validator.iter_errors(valid))
            and bool(list(validator.iter_errors(omitted)))
            and bool(list(validator.iter_errors({**valid, "project": ""})))
            and bool(list(validator.iter_errors({**valid, "project": "   "})))
        )
    ok(all(blank_contract_rejected)
       and all(checked_in_instance_results),
       "all five complete checked-in schemas require project and reject omitted, empty, and whitespace-only scope")

    direct_blank_results = [
        enrollment_command.begin_mapping_result(
            blank_contract_cases[0][1], actor="test", principal_id="test"),
        enrollment_command.complete_mapping_result(blank_contract_cases[1][1]),
        enrollment_command.finalize_mapping_result(
            blank_contract_cases[2][1], actor="test", principal_id="test"),
        enrollment_command.rotate_mapping_result(
            blank_contract_cases[3][1], actor="test", principal_id="test"),
        enrollment_command.revoke_mapping_result(
            blank_contract_cases[4][1], actor="test"),
    ]
    ok(all(result.get("error_code") == "invalid_agent_host_enrollment"
           for result in direct_blank_results),
       "transport-neutral enrollment commands reject blank project scope")

    mcp_project_parameter = inspect.signature(
        agent_tools.begin_agent_host_enrollment).parameters["project"]
    mcp_list_project_parameter = inspect.signature(
        agent_tools.list_agent_host_enrollments).parameters["project"]
    try:
        agent_tools.begin_agent_host_enrollment(None, "user-adapter18", " ")
        mcp_blank_rejected = False
    except ValueError as exc:
        mcp_blank_rejected = str(exc) == "project required"
    try:
        agent_tools.list_agent_host_enrollments(None, " ")
        mcp_list_blank_rejected = False
    except ValueError as exc:
        mcp_list_blank_rejected = str(exc) == "project required"
    ok(mcp_project_parameter.default is inspect.Signature.empty
       and mcp_list_project_parameter.default is inspect.Signature.empty
       and mcp_blank_rejected and mcp_list_blank_rejected,
       "MCP enrollment create and list require explicit non-blank project scope")

    install_required_args = [
        "install", "--bundle", str(TMP / "bundle-missing-project"),
        "--public-key", str(TMP / "public-missing-project.pem"),
        "--bootstrap-code-file", str(TMP / "bootstrap-missing-project.txt"),
        "--owner-user-id", "user-adapter18", "--no-start",
    ]
    with redirect_stderr(io.StringIO()):
        try:
            enrollment.main(install_required_args)
            cli_missing_project_rejected = False
        except SystemExit as exc:
            cli_missing_project_rejected = exc.code == 2
        try:
            enrollment.main(install_required_args + ["--project", " "])
            cli_blank_project_rejected = False
        except SystemExit as exc:
            cli_blank_project_rejected = exc.code == 2
    ok(cli_missing_project_rejected and cli_blank_project_rejected,
       "installer CLI rejects omitted and blank project scope")
    lifecycle_cli_args = {
        "rotate": ["--identity", "identity.json", "--config", "config.json"],
        "revoke": ["--identity", "identity.json", "--config", "config.json",
                   "--state", "state.json"],
        "uninstall": ["--identity", "identity.json", "--config", "config.json",
                      "--state", "state.json"],
    }
    lifecycle_cli_scope_rejected = []
    with redirect_stderr(io.StringIO()):
        for command, arguments in lifecycle_cli_args.items():
            for suffix in ([], ["--project", " "]):
                try:
                    enrollment.main([command, *arguments, *suffix])
                    lifecycle_cli_scope_rejected.append(False)
                except SystemExit as exc:
                    lifecycle_cli_scope_rejected.append(exc.code == 2)
    ok(all(lifecycle_cli_scope_rejected),
       "rotate, revoke, and uninstall CLI reject omitted and blank project scope")
    default_provider = client.post("/ixp/v1/agent-host-enrollments", json={
        "project": PROJECT,
        "owner_user_id": "user-adapter18",
        "requested_host_id": "host/adapter18-default-provider",
    })
    ok(default_provider.status_code == 200
       and default_provider.json()["enrollment"]["provider_allowlist"]
       == ["openai-codex"],
       "default enrollment admits the personal Codex provider without caller overrides")
    co_lane = client.post("/ixp/v1/agent-host-enrollments", json={
        "project": PROJECT,
        "owner_user_id": "user-adapter18",
        "requested_host_id": "host/co16-personal",
        "lane_allowlist": ["CO"],
    })
    ok(co_lane.status_code == 200
       and co_lane.json()["enrollment"]["execution_policy"]["lanes"] == ["CO"],
       "operator-authorized enrollment preserves the requested CO lane in server policy")
    mismatched_projects = client.post("/ixp/v1/agent-host-enrollments", json={
        "project": PROJECT,
        "owner_user_id": "user-adapter18",
        "requested_host_id": "host/adapter18-project-union",
        "project_allowlist": ["another-project"],
    })
    ok(mismatched_projects.status_code == 200
       and mismatched_projects.json()["enrollment"]["project_allowlist"]
       == ["another-project", PROJECT],
       "enrollment always admits its own project when callers provide an allowlist")
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
    preflight_environments = []

    def capture_secret_free_preflight(command, **kwargs):
        preflight_environments.append(dict(kwargs.get("env") or {}))
        output = ("codex-cli 1.2.3\n" if command[-1] == "--version"
                  else "Logged in using ChatGPT\n")
        return subprocess.CompletedProcess(command, 0, output, "")

    with patch.dict(os.environ, {
            "PM_MCP_TOKEN": "host-bearer-must-not-cross-preflight",
            "SWITCHBOARD_TOKEN": "alternate-bearer-must-not-cross-preflight",
    }):
        enrollment.preflight_codex_local_auth(
            codex_executable=str(TEST_CODEX), runner=capture_secret_free_preflight)
    ok(all("PM_MCP_TOKEN" not in environment
           and "SWITCHBOARD_TOKEN" not in environment
           for environment in preflight_environments),
       "local-auth probes never inherit the stable host coordination bearer")
    semver_precedence = [
        "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
        "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
        "1.0.0-rc.1", "1.0.0",
    ]
    semver_ordered = all(
        enrollment._parse_version(left) < enrollment._parse_version(right)
        for left, right in zip(semver_precedence, semver_precedence[1:])
    )
    try:
        enrollment._parse_version("1.0.0-rc.01")
        invalid_prerelease_denied = False
    except enrollment.EnrollmentError:
        invalid_prerelease_denied = True
    ok(semver_ordered
       and enrollment._parse_version("1.0.0+build.1")
       == enrollment._parse_version("1.0.0+build.2")
       and invalid_prerelease_denied,
       "signed updates implement full SemVer prerelease precedence and ignore build metadata")
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

    overlap_denials = []
    for name, mutate in (
            ("codex-equals-state", lambda value: value.update({
                "codex_home": value["state_root"]})),
            ("workspace-contains-codex", lambda value: value.update({
                "workspace_root": value["state_root"] / "shared",
                "codex_home": value["state_root"] / "shared" / "codex-home"})),
            ("prefix-inside-state", lambda value: value.update({
                "prefix": value["state_root"] / "releases"})),
            ("prefix-contains-source-codex", lambda value: value.update({
                "prefix": TEST_USER_CODEX_HOME.parent})),
            ("service-aliases-identity", lambda value: value.update({
                "service_path": value["config_root"] / "identity.json"})),
            ("service-aliases-state", lambda value: value.update({
                "service_path": value["state_root"] / "state.json"})),
            ("service-aliases-current-release", lambda value: value.update({
                "service_path": value["prefix"] / "current"})),
    ):
        unsafe_paths = paths(f"unsafe-layout-{name}")
        mutate(unsafe_paths)
        try:
            enrollment.install_host(
                bundle_dir=bundle_020, public_key_path=public_path,
                bootstrap_code="ahb-layout-not-consumed",
                base_url="https://switchboard.test", project=PROJECT,
                owner_user_id="user-adapter18", target_platform="linux",
                paths=unsafe_paths, http=http, service_runner=fake_service,
                local_auth_runner=fake_codex, codex_executable=str(TEST_CODEX),
                start_service=False)
            overlap_denials.append(False)
        except enrollment.EnrollmentError:
            overlap_denials.append(True)
    ok(all(overlap_denials),
       "install rejects overlapping lifecycle, auth, workspace, and release roots")

    serialized_paths = paths("serialized-install")
    active_installs = [0]
    max_active_installs = [0]

    def observed_install(**_kwargs):
        active_installs[0] += 1
        max_active_installs[0] = max(max_active_installs[0], active_installs[0])
        time.sleep(0.05)
        active_installs[0] -= 1
        return {"installed": True}

    def call_serialized_install(_index):
        return enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code="ahb-lock-proof", base_url="https://switchboard.test",
            project=PROJECT, owner_user_id="user-adapter18",
            target_platform="linux", paths=serialized_paths,
            start_service=False)

    with patch.object(enrollment, "_install_host_unlocked", observed_install):
        with ThreadPoolExecutor(max_workers=2) as pool:
            serialized_results = list(pool.map(call_serialized_install, range(2)))
    ok(max_active_installs[0] == 1
       and all(result.get("installed") for result in serialized_results),
       "install holds one interprocess lock across detection through finalization")

    orphan_paths = paths("orphan-identity")
    orphan_identity = orphan_paths["config_root"] / "identity.json"
    orphan_identity.parent.mkdir(parents=True)
    orphan_identity.write_text('{"orphan":"preserve-for-operator"}\n', encoding="utf-8")
    orphan_before = orphan_identity.read_bytes()
    try:
        enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code="ahb-orphan-not-consumed",
            base_url="https://switchboard.test", project=PROJECT,
            owner_user_id="user-adapter18", target_platform="linux",
            paths=orphan_paths, http=http, service_runner=fake_service,
            local_auth_runner=fake_codex, codex_executable=str(TEST_CODEX),
            start_service=False)
        orphan_denied = False
    except enrollment.EnrollmentError:
        orphan_denied = True
    ok(orphan_denied and orphan_identity.read_bytes() == orphan_before
       and not (orphan_paths["state_root"] / "codex-home").exists()
       and not (orphan_paths["state_root"] / "state.json").exists(),
       "orphan identity is rejected before copying auth and preserved for operator recovery")

    dangling_denials = []
    for dangling_name, dangling_kind in (
            ("dangling-identity", "identity"), ("dangling-state", "state")):
        dangling_paths = paths(dangling_name)
        dangling_identity = dangling_paths["config_root"] / "identity.json"
        dangling_state = dangling_paths["state_root"] / "state.json"
        dangling_artifact = dangling_identity if dangling_kind == "identity" else dangling_state
        dangling_artifact.parent.mkdir(parents=True)
        dangling_artifact.symlink_to(dangling_artifact.parent / "missing-target.json")
        try:
            enrollment.install_host(
                bundle_dir=bundle_020, public_key_path=public_path,
                bootstrap_code=f"ahb-{dangling_name}-not-consumed",
                base_url="https://switchboard.test", project=PROJECT,
                owner_user_id="user-adapter18", target_platform="linux",
                paths=dangling_paths, http=http, service_runner=fake_service,
                local_auth_runner=fake_codex, codex_executable=str(TEST_CODEX),
                start_service=False)
            dangling_denials.append(False)
        except enrollment.EnrollmentError:
            dangling_denials.append(
                dangling_artifact.is_symlink()
                and not (dangling_paths["state_root"] / "codex-home").exists())
    ok(all(dangling_denials),
       "dangling identity and state symlinks are rejected and preserved before auth copy")

    malformed_retry_paths = paths("malformed-retry-artifacts")
    malformed_identity = malformed_retry_paths["config_root"] / "identity.json"
    malformed_state = malformed_retry_paths["state_root"] / "state.json"
    malformed_identity.parent.mkdir(parents=True)
    malformed_state.parent.mkdir(parents=True)
    malformed_identity.write_text(json.dumps({
        "schema": enrollment.IDENTITY_SCHEMA,
        "status": "pending_enrollment",
        "private_key_pem": "durable-but-unmatched-private-key",
        "public_key_fingerprint": "sha256:" + "4" * 64,
        "completion_recovery_secret": "ahr-" + "r" * 43,
    }), encoding="utf-8")
    malformed_state.write_text(json.dumps({
        "schema": enrollment.LOCAL_STATE_SCHEMA,
        "status": "prepared_for_enrollment",
        "project": "wrong-project",
    }), encoding="utf-8")
    malformed_identity_before = malformed_identity.read_bytes()
    malformed_state_before = malformed_state.read_bytes()
    try:
        enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code="ahb-malformed-retry-not-consumed",
            base_url="https://switchboard.test", project=PROJECT,
            owner_user_id="user-adapter18", target_platform="linux",
            paths=malformed_retry_paths, http=http, service_runner=fake_service,
            local_auth_runner=fake_codex, codex_executable=str(TEST_CODEX),
            start_service=False)
        malformed_retry_denied = False
    except enrollment.EnrollmentError:
        malformed_retry_denied = True
    ok(malformed_retry_denied
       and malformed_identity.read_bytes() == malformed_identity_before
       and malformed_state.read_bytes() == malformed_state_before
       and not (malformed_retry_paths["state_root"] / "codex-home").exists(),
       "malformed retry artifacts are rejected before copying dedicated Codex auth")

    invalid_key_paths = paths("invalid-retry-key")
    invalid_key_identity = invalid_key_paths["config_root"] / "identity.json"
    invalid_key_state = invalid_key_paths["state_root"] / "state.json"
    invalid_key_identity.parent.mkdir(parents=True)
    invalid_key_state.parent.mkdir(parents=True)
    invalid_key_bootstrap = "ahb-invalid-retry-key-not-consumed"
    invalid_key_identity.write_text(json.dumps({
        "schema": enrollment.IDENTITY_SCHEMA,
        "status": "pending_enrollment",
        "private_key_pem": "not-a-private-key",
        "public_key_fingerprint": "sha256:" + "5" * 64,
        "completion_recovery_secret": "ahr-" + "k" * 43,
    }), encoding="utf-8")
    invalid_key_state.write_text(json.dumps({
        "schema": enrollment.LOCAL_STATE_SCHEMA,
        "status": "prepared_for_enrollment",
        "version": "0.2.0",
        "platform": "linux",
        "project": PROJECT,
        "bootstrap_fingerprint": "sha256:" + enrollment.hashlib.sha256(
            invalid_key_bootstrap.encode()).hexdigest(),
        "prefix": str(invalid_key_paths["prefix"]),
        "service_path": str(invalid_key_paths["service_path"]),
        "identity_path": str(invalid_key_identity),
        "config_path": str(invalid_key_paths["config_root"] / "config.json"),
        "state_path": str(invalid_key_state),
        "base_url": "https://switchboard.test",
    }), encoding="utf-8")
    try:
        enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code=invalid_key_bootstrap,
            base_url="https://switchboard.test", project=PROJECT,
            owner_user_id="user-adapter18", target_platform="linux",
            paths=invalid_key_paths, http=http, service_runner=fake_service,
            local_auth_runner=fake_codex, codex_executable=str(TEST_CODEX),
            start_service=False)
        invalid_key_denied = False
    except enrollment.EnrollmentError:
        invalid_key_denied = True
    ok(invalid_key_denied
       and not (invalid_key_paths["state_root"] / "codex-home").exists(),
       "retry identity private key must parse and match its public fingerprint before auth copy")

    preflight_failure_paths = paths("preflight-auth-rollback")

    def fail_local_auth(_command, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "codex unavailable")

    try:
        enrollment.install_host(
            bundle_dir=bundle_020, public_key_path=public_path,
            bootstrap_code="ahb-preflight-failure-not-consumed",
            base_url="https://switchboard.test", project=PROJECT,
            owner_user_id="user-adapter18", target_platform="linux",
            paths=preflight_failure_paths, http=http, service_runner=fake_service,
            local_auth_runner=fail_local_auth, codex_executable=str(TEST_CODEX),
            start_service=False)
        preflight_failure_visible = False
    except enrollment.EnrollmentError:
        preflight_failure_visible = True
    ok(preflight_failure_visible
       and not (preflight_failure_paths["state_root"] / "codex-home").exists()
       and not (preflight_failure_paths["state_root"] / "state.json").exists()
       and (TEST_USER_CODEX_HOME / "auth.json").is_file(),
       "failed pre-journal auth preflight rolls back only the dedicated credential copy")

    state_journal_failure_paths = paths("state-journal-auth-rollback")
    original_atomic_json = enrollment._atomic_json

    def fail_state_journal(path, payload, mode):
        if Path(path) == state_journal_failure_paths["state_root"] / "state.json":
            raise OSError("simulated state journal durability failure")
        return original_atomic_json(path, payload, mode)

    try:
        with patch.object(enrollment, "_atomic_json", fail_state_journal):
            enrollment.install_host(
                bundle_dir=bundle_020, public_key_path=public_path,
                bootstrap_code="ahb-state-journal-failure-not-consumed",
                base_url="https://switchboard.test", project=PROJECT,
                owner_user_id="user-adapter18", target_platform="linux",
                paths=state_journal_failure_paths, http=http,
                service_runner=fake_service, local_auth_runner=fake_codex,
                codex_executable=str(TEST_CODEX), start_service=False)
        state_journal_failure_visible = False
    except OSError:
        state_journal_failure_visible = True
    ok(state_journal_failure_visible
       and not (state_journal_failure_paths["state_root"] / "codex-home").exists()
       and not (state_journal_failure_paths["config_root"] / "identity.json").exists()
       and not (state_journal_failure_paths["state_root"] / "state.json").exists(),
       "state-journal failure rolls back the copied credential and pending identity")

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
    ok(durability_denied
       and not (durability_paths["state_root"] / "codex-home").exists()
       and not (durability_paths["config_root"] / "identity.json").exists()
       and not (durability_paths["state_root"] / "state.json").exists()
       and (TEST_USER_CODEX_HOME / "auth.json").is_file(),
       "every fresh-install failure before the lifecycle journal rolls back copied secrets")
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
    durability_conflict = client.post(
        "/ixp/v1/agent-host-enrollments/revoke",
        headers={"Authorization": f"Bearer {durability_completion['host_token']}"},
        json={"project": PROJECT, "host_id": "host/adapter18-durability",
              "reason": "must_not_change_terminal_state", "final_status": "uninstalled"},
    )
    durability_replay = client.post(
        "/ixp/v1/agent-host-enrollments/revoke",
        headers={"Authorization": f"Bearer {durability_completion['host_token']}"},
        json={"project": PROJECT, "host_id": "host/adapter18-durability",
              "reason": "exact_terminal_readback", "final_status": "revoked"},
    )
    durability_record = store.get_agent_host_enrollment(
        "host/adapter18-durability", project=PROJECT)
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
    ok(durability_conflict.json().get("error_code") == "terminal_status_conflict"
       and durability_conflict.json().get("status") == "revoked"
       and durability_replay.json().get("idempotent_replay") is True
       and durability_record.get("status") == "revoked",
       "revoked bearer can replay only the exact immutable terminal result")

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

    missing_id_bootstrap = begin("host/adapter18-missing-enrollment-id")
    missing_id_paths = paths("missing-enrollment-id")
    missing_id_effects = []

    def missing_id_http(*args, **kwargs):
        result = http(*args, **kwargs)
        if str(args[1]).endswith("/ixp/v1/agent-host-enrollments/complete"):
            result = dict(result)
            result["enrollment"] = dict(result.get("enrollment") or {})
            result["enrollment"].pop("enrollment_id", None)
        return result

    def missing_id_service(*args, **kwargs):
        missing_id_effects.append((args, kwargs))
        return fake_service(*args, **kwargs)

    missing_id_rejected = []
    for _ in range(2):
        try:
            enrollment.install_host(
                bundle_dir=bundle_020, public_key_path=public_path,
                bootstrap_code=missing_id_bootstrap["bootstrap_code"],
                base_url="https://switchboard.test", project=PROJECT,
                owner_user_id="user-adapter18", target_platform="linux",
                paths=missing_id_paths, http=missing_id_http,
                service_runner=missing_id_service, local_auth_runner=fake_codex,
                codex_executable=str(TEST_CODEX), start_service=True)
            missing_id_rejected.append(False)
        except enrollment.EnrollmentError:
            missing_id_rejected.append(True)
    missing_id_state = json.loads(
        (missing_id_paths["state_root"] / "state.json").read_text())
    missing_id_identity = json.loads(
        (missing_id_paths["config_root"] / "identity.json").read_text())
    ok(all(missing_id_rejected)
       and missing_id_state["status"] == "enrollment_response_incomplete"
       and missing_id_identity["status"] == "pending_enrollment"
       and not (missing_id_paths["config_root"] / "config.json").exists()
       and not missing_id_paths["service_path"].exists()
       and not (missing_id_paths["state_root"] / "workspaces").exists()
       and not missing_id_effects,
       "fresh and recovered completion responses without enrollment_id cannot finalize locally")

    defensive_finalization_rejections = []
    defensive_finalization_effects = []
    for finalization_status in enrollment._FINALIZATION_STATUSES:
        defensive_state = {
            "status": finalization_status,
            "pending_identity": {"host_id": "host/malformed", "host_token": "secret"},
            "pending_config": {"base_url": "https://switchboard.test", "project": PROJECT},
        }
        try:
            enrollment._finalize_install(
                state=defensive_state,
                state_path=TMP / "missing-id-defensive" / "state.json",
                identity_path=TMP / "missing-id-defensive" / "identity.json",
                config_path=TMP / "missing-id-defensive" / "config.json",
                target_platform="linux", prefix=TMP / "missing-id-defensive" / "prefix",
                service_path=TMP / "missing-id-defensive" / "agent-host.service",
                log_root=TMP / "missing-id-defensive" / "logs",
                state_root=TMP / "missing-id-defensive" / "state",
                entrypoint="adapters/agent_host.py", start_service=True,
                http=lambda *a, **k: defensive_finalization_effects.append((a, k)),
                service_runner=lambda *a, **k: defensive_finalization_effects.append((a, k)),
            )
            defensive_finalization_rejections.append(False)
        except enrollment.EnrollmentError:
            defensive_finalization_rejections.append(True)
    ok(all(defensive_finalization_rejections)
       and not defensive_finalization_effects
       and not (TMP / "missing-id-defensive").exists(),
       "every finalization status rejects a missing enrollment_id before local effects")

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
        project=PROJECT, http=http, service_runner=fake_service)
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
    mac_codex_home = Path(mac_config["codex_home"])
    initial_mac_token = mac_identity["host_token"]
    ok(mac_install["installed"] and stat.S_IMODE(mac_identity_path.stat().st_mode) == 0o600,
       "fresh macOS enrollment installs a 0600 rotatable identity")
    ok(mac_config["owner_user_id"] == "user-adapter18"
       and mac_config["tenant_allowlist"] == ["tenant-adapter18"]
       and mac_config["provider_allowlist"] == ["openai-codex"]
       and mac_config["lanes"] == ["ADAPTER"]
       and mac_config["capabilities"] == ["docs", "github", "python", "tests"]
       and mac_config["max_sessions"] == 8
       and mac_config["personal_wakes_only"] is False
       and mac_config["platform"] == "darwin"
       and mac_config["service_path"] == str(mac_paths["service_path"])
       and mac_codex_home == (mac_paths["state_root"] / "codex-home").resolve()
       and mac_codex_home != TEST_USER_CODEX_HOME.resolve()
       and json.loads((mac_codex_home / "auth.json").read_text())["tokens"][
           "access_token"] == "user-login-seed"
       and stat.S_IMODE(mac_codex_home.stat().st_mode) == 0o700
       and stat.S_IMODE((mac_codex_home / "auth.json").stat().st_mode) == 0o600,
       "installed policy comes only from the server-issued enrollment record")
    ok(codex_calls[:2] == [[str(TEST_CODEX), "--version"],
                           [str(TEST_CODEX), "login", "status"]]
       and mac_config["codex_executable"] == str(TEST_CODEX),
       "install proves the native Codex CLI and host-local ChatGPT login before bootstrap")
    ok(str(mac_codex_home) in codex_call_homes,
       "install verifies ChatGPT auth from the dedicated Agent Host Codex home")
    ok(mac_paths["service_path"].is_file()
       and b"LaunchAgents" not in mac_paths["service_path"].read_bytes()
       and service_calls[-1][:2] == ["launchctl", "bootstrap"],
       "macOS install renders and starts a per-user launchd service")

    cleanup_validation_results = []
    cleanup_http_calls = []
    mac_state_good = json.loads(mac_state_path.read_text())
    source_auth_before_cleanup_checks = (TEST_USER_CODEX_HOME / "auth.json").read_bytes()
    service_calls_before_cleanup_checks = len(service_calls)

    def cleanup_must_not_call_http(*args, **kwargs):
        cleanup_http_calls.append((args, kwargs))
        return http(*args, **kwargs)

    for cleanup_name, mutate_state, mutate_config, caller_identity in (
            ("codex-home-aliases-source", lambda value: None,
             lambda value: value.update({"codex_home": str(TEST_USER_CODEX_HOME)}),
             mac_identity_path),
            ("prefix-contains-source", lambda value: value.update({
                "prefix": str(TEST_USER_CODEX_HOME.parent)}),
             lambda value: value.update({
                 "repo_root": str(TEST_USER_CODEX_HOME.parent / "current")}),
             mac_identity_path),
            ("service-aliases-source-auth", lambda value: value.update({
                "service_path": str(TEST_USER_CODEX_HOME / "auth.json")}),
             lambda value: value.update({
                 "service_path": str(TEST_USER_CODEX_HOME / "auth.json")}),
             mac_identity_path),
            ("caller-identity-not-journaled", lambda value: None,
             lambda value: None, TEST_USER_CODEX_HOME / "auth.json"),
    ):
        unsafe_state = json.loads(json.dumps(mac_state_good))
        unsafe_config = json.loads(json.dumps(mac_config))
        mutate_state(unsafe_state)
        mutate_config(unsafe_config)
        enrollment._atomic_json(mac_state_path, unsafe_state, 0o600)
        enrollment._atomic_json(mac_config_path, unsafe_config, 0o600)
        unsafe_state_before = mac_state_path.read_bytes()
        try:
            enrollment.revoke_host(
                identity_path=caller_identity, config_path=mac_config_path,
                state_path=mac_state_path, project=PROJECT,
                http=cleanup_must_not_call_http, service_runner=fake_service)
            cleanup_validation_results.append(False)
        except enrollment.EnrollmentError:
            cleanup_validation_results.append(
                mac_state_path.read_bytes() == unsafe_state_before)
        # Restore the exact known-good installed artifacts for the next mutation.
        enrollment._atomic_json(mac_state_path, mac_state_good, 0o600)
        enrollment._atomic_json(mac_config_path, mac_config, 0o600)
    ok(all(cleanup_validation_results)
       and not cleanup_http_calls
       and len(service_calls) == service_calls_before_cleanup_checks
       and (TEST_USER_CODEX_HOME / "auth.json").read_bytes()
       == source_auth_before_cleanup_checks,
       "cleanup revalidates journal-bound roots and caller paths before any effect")

    lifecycle_calls_before_scope_mismatch = len(service_calls)
    lifecycle_state_before_scope_mismatch = mac_state_path.read_bytes()
    mismatched_lifecycle_scope_denied = []
    for operation in (
        lambda: enrollment.rotate_identity(
            identity_path=mac_identity_path, config_path=mac_config_path,
            project="another-project", http=http, service_runner=fake_service),
        lambda: enrollment.revoke_host(
            identity_path=mac_identity_path, config_path=mac_config_path,
            state_path=mac_state_path, project="another-project", http=http,
            service_runner=fake_service),
        lambda: enrollment.uninstall_host(
            identity_path=mac_identity_path, config_path=mac_config_path,
            state_path=mac_state_path, project="another-project", http=http,
            service_runner=fake_service),
    ):
        try:
            operation()
            mismatched_lifecycle_scope_denied.append(False)
        except enrollment.EnrollmentError as exc:
            mismatched_lifecycle_scope_denied.append("does not match" in str(exc))
    ok(all(mismatched_lifecycle_scope_denied)
       and len(service_calls) == lifecycle_calls_before_scope_mismatch
       and mac_state_path.read_bytes() == lifecycle_state_before_scope_mismatch,
       "local lifecycle mutations reject project mismatch before service or remote effects")

    consumed = store.complete_agent_host_enrollment(
        bootstrap_code=mac_bootstrap["bootstrap_code"], hostname="replay",
        platform="darwin", public_key_fingerprint="sha256:" + "1" * 64,
        completion_recovery_secret="ahr-" + "x" * 43,
        project=PROJECT)
    ok(consumed.get("error_code") == "bootstrap_code_consumed",
       "device bootstrap is single-use")

    registration_payload = {
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
            "limits": {"max_sessions": 8},
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
        }
    register = client.post("/ixp/v1/register_host", headers={
        "Authorization": f"Bearer {initial_mac_token}"}, json=registration_payload)
    ok(register.status_code == 200 and register.json().get("host_id") == "host/adapter18-macos",
       "enrolled principal registers only its exact host identity")
    store.ensure_org(store.DEFAULT_ORG_ID, "6th Element Labs", created_by="ui21-host-test")
    store.set_project_access(
        PROJECT, store.DEFAULT_ORG_ID, purpose="UI-21 host fixture",
        created_by="ui21-host-test")
    store.ensure_user(
        "user-adapter18", "user-adapter18@example.test", "UI-21 owner",
        created_by="ui21-host-test")
    store.add_org_member(
        store.DEFAULT_ORG_ID, "user-adapter18", role="member",
        created_by="ui21-host-test")
    raw_api_key = "sk-ui21-host-only-super-secret"
    api_result = enrollment.enroll_api_key(
        identity_path=mac_identity_path,
        config_path=mac_config_path,
        project=PROJECT,
        provider="openai-codex",
        provider_account_id="acct-openai-ui21",
        billing_account_id="billing-openai-ui21",
        budget_ceiling=25,
        budget_currency="usd",
        api_key=raw_api_key,
        http=http,
    )
    api_ref = api_result.get("execution_connection_id")
    api_metadata = credential_repository.get_metadata(
        api_ref, principal_user_id="user-adapter18", project=PROJECT)
    ok(api_result.get("enrolled") is True
       and api_result.get("credential_values_redacted") is True
       and api_metadata.get("connection_kind") == "direct_api"
       and api_metadata.get("billing_account_bound") is True
       and (api_metadata.get("budget_policy") or {}).get("ceiling") == 25
       and raw_api_key not in json.dumps(api_result, sort_keys=True)
       and raw_api_key not in json.dumps(api_metadata, sort_keys=True),
       "host-authenticated API enrollment immediately vaults the key and returns only redacted billing/budget metadata")
    denied_api = client.post(
        "/ixp/v1/agent-host-provider-connections/enroll-api-key",
        headers={"Authorization": "Bearer invalid-host-token"},
        json={
            "project": PROJECT,
            "host_id": "host/adapter18-macos",
            "provider": "openai-codex",
            "provider_account_id": "acct-denied",
            "billing_account_id": "billing-denied",
            "budget_ceiling": 10,
            "budget_currency": "USD",
            "api_key": raw_api_key,
        },
    )
    ok(denied_api.status_code in (401, 403)
       and raw_api_key not in denied_api.text,
       "the one-use secret endpoint rejects a non-host bearer without echoing the key")
    invalid_api = client.post(
        "/ixp/v1/agent-host-provider-connections/enroll-api-key",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={
            "project": PROJECT,
            "host_id": "host/adapter18-macos",
            "provider": "openai-codex",
            "provider_account_id": "acct-invalid-budget",
            "billing_account_id": "billing-invalid-budget",
            "budget_ceiling": "NaN",
            "budget_currency": "USD",
            "api_key": raw_api_key,
        },
    )
    ok(invalid_api.status_code == 422 and raw_api_key not in invalid_api.text,
       "validation failures scrub the one-use key while rejecting non-finite budgets")

    cli_capture = {}

    def capture_cli_api_key(**kwargs):
        cli_capture.update(kwargs)
        return {
            "enrolled": True,
            "provider": "openai-codex",
            "connection_kind": "direct_api",
            "execution_connection_id": "execconn-cli-redacted",
            "credential_values_redacted": True,
        }

    cli_stdout = io.StringIO()
    cli_stderr = io.StringIO()
    with (patch.object(enrollment, "enroll_api_key", capture_cli_api_key),
          patch("sys.stdin", io.StringIO(raw_api_key + "\n")),
          redirect_stdout(cli_stdout), redirect_stderr(cli_stderr)):
        cli_rc = enrollment.main([
            "enroll-api-key",
            "--identity", str(mac_identity_path),
            "--config", str(mac_config_path),
            "--project", PROJECT,
            "--provider", "openai-codex",
            "--provider-account", "acct-openai-ui21",
            "--billing-account", "billing-openai-ui21",
            "--budget-ceiling", "25",
            "--budget-currency", "usd",
            "--api-key-stdin",
        ])
    cli_output = cli_stdout.getvalue() + cli_stderr.getvalue()
    ok(cli_rc == 0 and cli_capture.get("api_key") == raw_api_key
       and raw_api_key not in cli_output
       and raw_api_key not in mac_identity_path.read_text()
       and raw_api_key not in mac_config_path.read_text(),
       "the real enroll-api-key CLI consumes stdin, never argv/env/disk, and emits only a redacted receipt")
    unavailable_auth = {
        "available": False, "runtime": "codex",
        "auth_mode": "chatgpt_personal",
        "account_fingerprint": None,
        "credential_values_redacted": True,
        "provider_credential_exported": False,
        "unavailable_reason": "EnrollmentError",
    }
    unavailable_registration = json.loads(json.dumps(registration_payload))
    unavailable_registration["runtimes"][0]["local_auth"] = unavailable_auth
    unavailable_registration["capacity"]["local_auth"] = unavailable_auth
    unavailable_register = client.post(
        "/ixp/v1/register_host",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json=unavailable_registration,
    )
    unavailable_runtime = unavailable_register.json().get("runtimes", [{}])[0]
    restored_register = client.post(
        "/ixp/v1/register_host",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json=registration_payload,
    )
    ok(unavailable_register.status_code == 200
       and unavailable_runtime.get("local_auth", {}).get("available") is False
       and not coordination_store._runtime_matches_selector(
           unavailable_runtime, {"runtime": "codex"})
       and restored_register.status_code == 200,
       "an auth-loss refresh is accepted, made ineligible, and recoverable")
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
    exact_runner_connection = {
        "execution_connection_id": "execconn-runner-exact",
        "wake_id": "wake-runner-exact", "task_id": "ADAPTER-18",
        "claim_id": "taskclaim-runner-exact",
        "work_session_id": "worksession-runner-exact",
        "runner_session_id": "run-personal-exact",
        "host_id": "host/adapter18-macos",
        "host_principal_id": host_principal["id"],
        "agent_id": "codex/personal-exact", "source_sha": "c" * 40,
    }
    runner_now = enrollment_store.time.time()
    with enrollment_store._conn(PROJECT) as connection:
        connection.execute(
            "INSERT INTO personal_execution_connections("
            "execution_connection_id, wake_id, task_id, claim_id, work_session_id, "
            "runner_session_id, host_id, host_principal_id, agent_id, source_sha, "
            "status, created_at, expires_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'reserved',?,?,?)",
            (*exact_runner_connection.values(), runner_now, runner_now + 3600, runner_now),
        )
    exact_runner_registration = client.post(
        "/ixp/v1/register_runner_session",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={
            "project": PROJECT,
            **{key: exact_runner_connection[key] for key in (
                "runner_session_id", "host_id", "agent_id", "task_id", "claim_id")},
            "runtime": "codex", "status": "starting",
            "metadata": {key: exact_runner_connection[key] for key in (
                "wake_id", "work_session_id", "source_sha", "execution_connection_id")},
        },
    )
    native_wake = store.request_wake(
        selector={"runtime": "codex", "agent_id": "codex/native-exact",
                  "task_id": "ADAPTER-18"},
        reason="native host-local admission", source="test", task_id="ADAPTER-18",
        policy={"require_runner_bind": True,
                "scheduler": {"mode": "hybrid", "prefer_persistent": True,
                              "allow_persistent": True, "allow_ephemeral": True}},
        actor="test", project=PROJECT,
    )
    with enrollment_store._conn(PROJECT) as connection:
        connection.execute(
            "UPDATE wake_intents SET placement_json=? WHERE wake_id=?",
            (json.dumps({"selected_host_id": "host/adapter18-macos"}),
             native_wake["wake_id"]),
        )
    native_preclaim_registration = client.post(
        "/ixp/v1/register_runner_session",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={
            "project": PROJECT, "runner_session_id": "run-native-exact",
            "host_id": "host/adapter18-macos", "agent_id": "codex/native-exact",
            "runtime": "codex", "task_id": "ADAPTER-18", "status": "starting",
            "metadata": {"wake_id": native_wake["wake_id"],
                         "credential_admission_phase": "preclaim"},
        },
    )
    native_claim_bound_registration = client.post(
        "/ixp/v1/register_runner_session",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={
            "project": PROJECT, "runner_session_id": "run-native-exact",
            "host_id": "host/adapter18-macos", "agent_id": "codex/native-exact",
            "runtime": "codex", "task_id": "ADAPTER-18",
            "claim_id": "taskclaim-native-exact", "status": "running",
            "require_task_bind": True,
            "metadata": {"wake_id": native_wake["wake_id"],
                         "work_session_id": "worksession-native-exact",
                         "credential_admission_phase": "claim_bound"},
        },
    )
    unbound_runner_creation = client.post(
        "/ixp/v1/register_runner_session",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={
            "project": PROJECT, "runner_session_id": "run-personal-fabricated",
            "host_id": "host/adapter18-macos", "agent_id": "codex/fabricated",
            "runtime": "codex", "task_id": "ADAPTER-18",
            "claim_id": "taskclaim-fabricated", "status": "running",
            "metadata": {
                "wake_id": "wake-fabricated", "work_session_id": "worksession-fabricated",
                "source_sha": "d" * 40,
                "execution_connection_id": "execconn-fabricated",
            },
        },
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
       and exact_runner_registration.status_code == 200
       and native_preclaim_registration.status_code == 200
       and native_preclaim_registration.json().get("metadata", {}).get(
           "native_host_execution") is True
       and native_claim_bound_registration.status_code == 200
       and unbound_runner_creation.status_code == 403
       and unbound_runner_creation.json()["detail"].get("error_code")
       == "runner_execution_binding_mismatch"
       and atomic_runner_hijack.get("error_code") == "runner_identity_mismatch"
       and runner_after_hijack.get("principal_id") == victim_principal["id"]
       and exact_claim_completion.status_code == 200
       and wrong_claim_completion.status_code == 403
       and exact_claim_abandon.status_code == 200
       and exact_session_checkpoint.status_code == 200
       and wrong_session_checkpoint.status_code == 403,
       "enrolled bearer is fenced to its host, runner identity, and exact terminal tuple")

    readback_binding = {
        "task_id": "ADAPTER-18", "claim_id": "taskclaim-postreadback",
        "work_session_id": "worksession-postreadback",
        "runner_session_id": "run-postreadback",
        "host_id": "host/adapter18-macos", "agent_id": "codex/postreadback",
        "wake_id": "wake-postreadback", "source_sha": "7" * 40,
        "execution_connection_id": "execconn-postreadback",
    }
    readback_head = "8" * 40
    readback_test_run = {
        "schema": "switchboard.executed_test_run.v1",
        "status": "success", "executed": True, "exit_code": 0,
    }
    readback_evidence = {
        "branch": "codex/ADAPTER-18-postreadback",
        "head_sha": readback_head,
        "executed_test_run": readback_test_run,
    }
    readback_now = enrollment_store.time.time()
    with enrollment_store._conn(PROJECT) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO tasks(task_id, title, status, created_at, updated_at) "
            "VALUES ('ADAPTER-18','post-processing readback','In Progress',?,?)",
            (readback_now, readback_now))
        connection.execute(
            "INSERT INTO task_claims(id, task_id, agent_id, principal_id, status, "
            "claimed_at, expires_at) VALUES (?,?,?,?, 'active',?,?)",
            (readback_binding["claim_id"], readback_binding["task_id"],
             readback_binding["agent_id"], host_principal["id"], readback_now,
             readback_now + 3600))
        connection.execute(
            "INSERT INTO work_sessions(work_session_id, project_id, task_id, claim_id, "
            "agent_id, runtime, repo_role, branch, head_sha, worktree_path, storage_mode, "
            "status, dirty_status, conflict_marker_count, hygiene_json, file_leases_json, "
            "resource_leases_json, env_json, policy_profile, created_by, updated_by, "
            "created_at, updated_at, expires_at) "
            "VALUES (?,?,?,?,?,'codex','canonical',?,?,?,'worktree','active','clean',0,"
            "'{}','[]','[]','{}','code_strict','test','test',?,?,?)",
            (readback_binding["work_session_id"], PROJECT,
             readback_binding["task_id"], readback_binding["claim_id"],
             readback_binding["agent_id"], readback_evidence["branch"],
             readback_binding["source_sha"], str(TMP / "postreadback-worktree"),
             readback_now, readback_now, readback_now + 3600))
        connection.execute(
            "INSERT INTO wake_intents(wake_id, source, reason, selector_json, policy_json, "
            "status, requested_at, claimed_at, claimed_by_host, completed_at, "
            "runner_session_id, agent_id, result_json, task_id, principal_id) "
            "VALUES (?,'test','post-processing readback','{}','{}','completed',?,?,?,?,?,?,?, ?,?)",
            (readback_binding["wake_id"], readback_now, readback_now,
             readback_binding["host_id"], readback_now,
             readback_binding["runner_session_id"], readback_binding["agent_id"],
             json.dumps({"started": True, "head_sha": readback_head,
                         "branch": readback_evidence["branch"]}, sort_keys=True),
             readback_binding["task_id"], host_principal["id"]))
        connection.execute(
            "INSERT INTO personal_execution_connections(execution_connection_id, wake_id, "
            "task_id, claim_id, work_session_id, runner_session_id, host_id, "
            "host_principal_id, agent_id, source_sha, status, created_at, expires_at, "
            "claimed_at, completed_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?)",
            (readback_binding["execution_connection_id"], readback_binding["wake_id"],
             readback_binding["task_id"], readback_binding["claim_id"],
             readback_binding["work_session_id"], readback_binding["runner_session_id"],
             readback_binding["host_id"], host_principal["id"],
             readback_binding["agent_id"], readback_binding["source_sha"],
             readback_now, readback_now + 3600,
             readback_now, readback_now, readback_now))
        connection.execute(
            "INSERT INTO runner_sessions(runner_session_id, host_id, agent_id, runtime, "
            "task_id, claim_id, status, cwd, control_json, metadata_json, "
            "last_snapshot_json, principal_id, started_at, heartbeat_at, heartbeat_ttl_s, "
            "updated_at) VALUES (?,?,?,'codex',?,?,'completed',?,'{}',?,'{}',?,?,?,180,?)",
            (readback_binding["runner_session_id"], readback_binding["host_id"],
             readback_binding["agent_id"], readback_binding["task_id"],
             readback_binding["claim_id"], str(TMP / "postreadback-worktree"),
             json.dumps({key: readback_binding[key] for key in (
                 "wake_id", "work_session_id", "source_sha",
                 "execution_connection_id")}, sort_keys=True),
             host_principal["id"], readback_now, readback_now, readback_now))
    original_coordination_conn = coordination_store._conn
    snapshot_requests = []

    @contextmanager
    def tracked_coordination_conn(*args, **kwargs):
        snapshot_requests.append(kwargs.get("read_snapshot") is True)
        with original_coordination_conn(*args, **kwargs) as connection:
            yield connection

    with patch.object(coordination_store, "_conn", tracked_coordination_conn):
        terminalized_readback = store.get_personal_execution_postprocessing_state(
            readback_binding, principal_id=host_principal["id"],
            completed_head_sha=readback_head, expected_evidence=readback_evidence,
            project=PROJECT)
    readback_hygiene = {
        "executed_test_run": readback_test_run,
        "personal_host_checkout": {
            "source_sha": readback_binding["source_sha"],
            "head_sha": readback_head,
        },
    }
    with enrollment_store._conn(PROJECT) as connection:
        connection.execute(
            "UPDATE work_sessions SET head_sha=?, hygiene_json=? WHERE work_session_id=?",
            (readback_head, json.dumps(readback_hygiene, sort_keys=True),
             readback_binding["work_session_id"]))
    checkpointed_readback = client.post(
        "/ixp/v1/personal_execution/postprocessing_state",
        headers={"Authorization": f"Bearer {initial_mac_token}"},
        json={"project": PROJECT, "binding": readback_binding,
              "completed_head_sha": readback_head,
              "expected_evidence": {
                  "branch": readback_evidence["branch"],
                  "executed_test_run": readback_test_run,
              }})
    with enrollment_store._conn(PROJECT) as connection:
        connection.execute(
            "UPDATE task_claims SET status='completed', completed_at=? WHERE id=?",
            (readback_now, readback_binding["claim_id"]))
        connection.execute(
            "UPDATE work_sessions SET status='completed', completed_at=? "
            "WHERE work_session_id=?",
            (readback_now, readback_binding["work_session_id"]))
        connection.execute(
            "UPDATE tasks SET status='In Review', updated_at=? WHERE task_id=?",
            (readback_now, readback_binding["task_id"]))
        connection.execute(
            "INSERT INTO task_git_state(task_id, branch, head_sha, pushed_at, "
            "evidence_json, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET branch=excluded.branch, "
            "head_sha=excluded.head_sha, pushed_at=excluded.pushed_at, "
            "evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
            (readback_binding["task_id"], readback_evidence["branch"], readback_head,
             readback_now, json.dumps(readback_evidence, sort_keys=True), readback_now))
    completed_readback = store.get_personal_execution_postprocessing_state(
        readback_binding, principal_id=host_principal["id"],
        completed_head_sha=readback_head, expected_evidence=readback_evidence,
        project=PROJECT)
    wrong_tuple_readback = store.get_personal_execution_postprocessing_state(
        {**readback_binding, "runner_session_id": "run-wrong"},
        principal_id=host_principal["id"], completed_head_sha=readback_head,
        expected_evidence=readback_evidence, project=PROJECT)
    ok(terminalized_readback.get("state") == "terminalized"
       and snapshot_requests == [True]
       and checkpointed_readback.status_code == 200
       and checkpointed_readback.json().get("state") == "checkpointed"
       and completed_readback.get("state") == "completed"
       and wrong_tuple_readback.get("allowed") is False,
       "one authenticated server snapshot verifies every post-processing tuple phase")
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

    def fail_rotated_service_start(command, **kwargs):
        del kwargs
        service_calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            1 if command[:2] == ["launchctl", "bootstrap"] else 0,
            "",
            "simulated start failure",
        )

    try:
        enrollment.rotate_identity(
            identity_path=mac_identity_path, config_path=mac_config_path,
            project=PROJECT, http=lose_rotation_response, service_runner=fake_service)
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
    try:
        enrollment.rotate_identity(
            identity_path=mac_identity_path, config_path=mac_config_path,
            project=PROJECT, http=http,
            service_runner=fail_rotated_service_start)
        rotation_start_failed = False
    except enrollment.EnrollmentError:
        rotation_start_failed = True
    consumed_rotation_recovery = store.get_agent_host_rotation_recovery_principal(
        token=initial_mac_token, host_id="host/adapter18-macos", project=PROJECT)
    pending_rotation = json.loads(mac_identity_path.read_text())
    rotated = enrollment.rotate_identity(
        identity_path=mac_identity_path, config_path=mac_config_path,
        project=PROJECT, http=http,
        service_runner=fake_service)
    mac_identity = json.loads(mac_identity_path.read_text())
    rotated_token = mac_identity["host_token"]
    ok(rotation_start_failed and consumed_rotation_recovery is None
       and pending_rotation["rotation_pending_restart"] is True
       and pending_rotation["host_token"] != initial_mac_token
       and rotated["identity_generation"] == 3 and rotated_token != initial_mac_token
       and rotated["service_restarted"] is True
       and rotated["resumed"] is True
       and "rotation_pending_restart" not in mac_identity
       and service_calls[-1][:3] == ["launchctl", "kickstart", "-k"],
       "bounded rotation recovery resumes a failed start without rotating again")
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
    bundle_022 = TMP / "bundle-0.2.2"
    enrollment.create_signed_bundle(ROOT, bundle_022, "0.2.2", private_path)
    failed_restart_calls = []

    def fail_new_then_restart_rollback(command, **kwargs):
        del kwargs
        failed_restart_calls.append(list(command))
        failed_new_restart = (
            command[:3] == ["launchctl", "kickstart", "-k"]
            and len([call for call in failed_restart_calls
                     if call[:3] == ["launchctl", "kickstart", "-k"]]) == 1
        )
        return subprocess.CompletedProcess(
            command, 1 if failed_new_restart else 0, "",
            "simulated update restart failure" if failed_new_restart else "")

    try:
        enrollment.update_host(
            bundle_dir=bundle_022, public_key_path=public_path,
            state_path=mac_state_path,
            service_runner=fail_new_then_restart_rollback)
        rollback_visible = False
    except enrollment.EnrollmentError:
        rollback_visible = True
    rollback_restarts = [
        call for call in failed_restart_calls
        if call[:3] == ["launchctl", "kickstart", "-k"]
    ]
    ok(rollback_visible and len(rollback_restarts) == 2
       and (mac_paths["prefix"] / "current").resolve().name == "0.2.1"
       and json.loads(mac_config_path.read_text())["agent_host_version"] == "0.2.1",
       "failed update restores and restarts the previous signed release")
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
            state_path=mac_state_path, project=PROJECT, http=offline,
            service_runner=fake_service)
        offline_visible = False
    except enrollment.EnrollmentError:
        offline_visible = True
    pending_state = json.loads(mac_state_path.read_text())
    service_calls_before_pending_update = len(service_calls)
    try:
        enrollment.update_host(
            bundle_dir=bundle_021, public_key_path=public_path,
            state_path=mac_state_path, service_runner=fake_service)
        pending_update_denied = False
    except enrollment.EnrollmentError as exc:
        pending_update_denied = "clean installed state" in str(exc)
    ok(offline_visible and pending_state["status"] == "revocation_response_unknown"
       and mac_identity_path.is_file()
       and pending_update_denied
       and len(service_calls) == service_calls_before_pending_update
       and (mac_paths["prefix"] / "current").resolve().name == "0.2.1",
       "offline revoke stops work but preserves retry identity as visible pending state")

    def lose_revoke_response(*args, **kwargs):
        response = http(*args, **kwargs)
        raise enrollment.EnrollmentError("simulated committed revoke response loss")

    try:
        enrollment.revoke_host(
            identity_path=mac_identity_path, config_path=mac_config_path,
            state_path=mac_state_path, project=PROJECT, http=lose_revoke_response,
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
        state_path=mac_state_path, project=PROJECT, http=http,
        service_runner=fake_service)
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
        mac_paths["config_root"], mac_paths["state_root"] / "provider-runtimes",
        mac_codex_home])
    ok(residue["residue_free"] and not mac_codex_home.exists(),
       "post-revoke identity, provider, and Codex-auth residue scan is clean")
    mac_uninstalled = enrollment.uninstall_host(
        identity_path=mac_identity_path, config_path=mac_config_path,
        state_path=mac_state_path, project=PROJECT, http=http,
        service_runner=fake_service)
    ok(mac_uninstalled.get("uninstalled") is True
       and not mac_paths["prefix"].exists()
       and not mac_paths["config_root"].exists(),
       "a completed revoke remains locally uninstallable without manual cleanup")

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
    linux_codex_home = Path(json.loads(linux_config.read_text())["codex_home"])
    service_text = linux_paths["service_path"].read_text()
    ok(linux_install["installed"] and "NoNewPrivileges=yes" in service_text
       and str(linux_paths["state_root"]) in service_text
       and str(linux_workspace_root) in service_text
       and str(linux_codex_home) in service_text
       and (linux_codex_home / "auth.json").is_file()
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
       and launched_env.get("PM_PERSONAL_AGENT_HOST_EXECUTION") == "0"
       and launched_env.get("PM_PERSONAL_AGENT_HOST_RECOVERY") == "1"
       and launched_env.get("PM_AUTH_HOST_CLASSES")
       == "trusted_private_worker,user_owned_persistent"
       and launched_env.get("PM_PERSONAL_WORKSPACE_ROOT") == str(linux_workspace_root)
       and launched_env.get("CODEX_HOME") == str(linux_codex_home.resolve())
       and launched_env.get("PM_AGENT_HOST_CODEX_HOME") == str(linux_codex_home.resolve())
       and launched_env.get("PM_AGENT_HOST_SOURCE_CODEX_HOME")
       and launched_env.get("PM_AGENT_HOST_USER_HOME") == str(Path.home().resolve())
       and launched_env.get("PM_AGENT_HOST_IDENTITY_PATH") == str(linux_identity.resolve())
       and launched_env.get("PM_AGENT_HOST_CONFIG_PATH") == str(linux_config.resolve())
       and launched_env.get("PM_AGENT_HOST_STATE_PATH") == str(linux_state.resolve())
       and all(isinstance(value, str) for value in launched_env.values()),
       "service-run strips metered keys and binds the OS test-sandbox protection paths")
    personal_auth = {
        "available": True,
        "runtime": "codex",
        "auth_mode": "chatgpt_personal",
        "account_fingerprint": "acct-1111111111111111",
        "credential_values_redacted": True,
        "provider_credential_exported": False,
    }
    auth_inventory = {
        "runtimes": [{"runtime": "codex", "local_auth": dict(personal_auth)}],
        "capacity": {"local_auth": dict(personal_auth)},
    }
    refreshed_proof = {
        "authenticated": True,
        "auth_mode": "chatgpt_personal",
        "account_fingerprint": "acct-2222222222222222",
    }
    with patch.object(agent_host, "preflight_codex_local_auth",
                      side_effect=[RuntimeError("signed out"), refreshed_proof]):
        auth_withdrawn = agent_host.refresh_local_auth_inventory(
            auth_inventory, force=True)
        withdrawn = dict(auth_inventory["capacity"]["local_auth"])
        auth_restored = agent_host.refresh_local_auth_inventory(
            auth_inventory, force=True)
    ok(auth_withdrawn and withdrawn.get("available") is False
       and auth_restored
       and auth_inventory["capacity"]["local_auth"].get("available") is True
       and auth_inventory["runtimes"][0]["local_auth"]
       == auth_inventory["capacity"]["local_auth"],
       "daemon re-probes personal login, withdraws unavailable auth, and restores it")

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
    coordinator_workspace.mkdir(parents=True)
    coordinator_marker = coordinator_workspace / "coordinator-owned.txt"
    coordinator_marker.write_text("must remain\n", encoding="utf-8")
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
    with patch.dict(os.environ, binding_environment):
        local_cleanup = switchboard_core._cleanup_personal_bound_workspace({
            "bound_existing": True,
            "workspace_path": str(materialized_workspace),
        })
    ok(local_cleanup.get("cleaned") is True
       and not materialized_workspace.exists() and coordinator_marker.is_file(),
       "personal cleanup removes only the adopted host-local checkout")

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
        "OPENAI_API_KEY", "PM_MCP_TOKEN", "SWITCHBOARD_TOKEN", "CODEX_HOME",
        "PM_AGENT_HOST_RUNNER_DIR", "PM_HOST_ID", "PM_PROJECT",
    )}
    local_git_heads = [source_sha, completed_sha]
    local_git_common = (TMP / "local-worker-git-common").resolve()
    local_git_dir = (local_git_common / "worktrees" / "local-worker").resolve()
    captured_local_codex: dict[str, object] = {}
    local_control_calls: list[tuple[str, dict]] = []
    local_completion_response_lost = [False]
    local_final_heartbeat_lost = [False]

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
        if args == ("ls-remote", "--exit-code", "--refs", "origin",
                    "refs/heads/codex/ADAPTER-18-local-worker"):
            return (completed_sha
                    + "\trefs/heads/codex/ADAPTER-18-local-worker")
        raise AssertionError(args)

    def fake_local_codex(command, **kwargs):
        captured_local_codex.update({"command": command, "kwargs": kwargs})
        return subprocess.CompletedProcess(command, 0, "native codex completed", "")

    def fake_local_control(method, path, body):
        ok(method == "POST", "native local worker uses authenticated state-changing calls")
        local_control_calls.append((path, dict(body)))
        if (path == "/ixp/v1/heartbeat_runner_session"
                and not local_final_heartbeat_lost[0]):
            local_final_heartbeat_lost[0] = True
            raise RuntimeError("simulated post-run heartbeat loss")
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

    unknown_completion_calls: list[tuple[str, str, dict | None]] = []

    def fake_unknown_completion_control(method, path, body):
        unknown_completion_calls.append((method, path, body))
        if method == "POST":
            raise RuntimeError("simulated committed completion response loss")
        ok(method == "GET" and path.startswith("/txp/v1/list_wake_intents?"),
           "outcome-unknown wake completion performs authenticated durable readback")
        return {"wake_intents": [{
            "wake_id": "wake-readback-recovery",
            "status": "completed",
        }]}

    with patch.object(codex_local_worker.time, "sleep", return_value=None):
        readback_terminal = codex_local_worker._complete_wake(
            fake_unknown_completion_control,
            {
                "wake_id": "wake-readback-recovery",
                "runner_session_id": "runner-readback-recovery",
                "agent_id": "codex/ADAPTER-18-readback",
                "host_id": "host/adapter18-linux",
            },
            {"started": True, "reason": "native_codex_execution_completed"},
        )
    ok(readback_terminal.get("status") == "completed"
       and readback_terminal.get("completion_confirmed_by_readback") is True
       and len([call for call in unknown_completion_calls if call[0] == "POST"]) == 3
       and len([call for call in unknown_completion_calls if call[0] == "GET"]) == 1,
       "lost wake-completion responses resume only after exact authoritative readback")

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
            "CODEX_HOME": str(linux_codex_home),
            "PM_AGENT_HOST_RUNNER_DIR": str(linux_paths["state_root"] / "runner"),
            "PM_HOST_ID": "host/adapter18-linux",
            "PM_PROJECT": PROJECT,
            "PM_CO_ACCOUNT_BINDING_JSON": json.dumps({
                "task_id": "ADAPTER-18",
                "claim_id": "taskclaim-local-worker",
                "work_session_id": "worksession-local-worker",
                "host_id": "host/adapter18-linux",
                "runner_session_id": "runner-local-worker",
                "agent_id": "codex/ADAPTER-18-local-worker",
            }),
            "OPENAI_API_KEY": "must-not-cross-local-worker-boundary",
            "PM_MCP_TOKEN": "stable-host-bearer-must-not-cross",
            "SWITCHBOARD_TOKEN": "alternate-host-bearer-must-not-cross",
        })
        local_evidence = codex_local_worker.run(
            local_task, runner=fake_local_codex,
            http=fake_local_control)
        local_lifecycle = local_evidence.pop(
            "_switchboard_personal_execution_lifecycle")
        local_success_was_deferred = not any(
            (path == "/txp/v1/complete_wake"
             or (path == "/ixp/v1/register_runner_session"
                 and body.get("status") == "completed"))
            for path, body in local_control_calls)
        local_terminal = local_lifecycle["complete"]({
            **local_evidence,
            "executed_test_run": {
                "schema": "switchboard.executed_test_run.v1",
                "status": "success", "executed": True, "exit_code": 0,
            },
        })
        recovery_root = linux_paths["state_root"] / "runner" / "postprocessing-recovery"
        receipt_files_before_restart = list(recovery_root.glob("*.json"))
        recovery_receipt_template = json.loads(
            receipt_files_before_restart[0].read_text(encoding="utf-8"))

        def fake_recovery_git(workspace, *args):
            ok(workspace == str(local_workspace),
               "restart recovery stays in its exact managed workspace")
            if args == ("rev-parse", "HEAD"):
                return completed_sha
            if args == ("status", "--porcelain"):
                return ""
            raise AssertionError(f"unexpected recovery git command: {args!r}")

        with (patch.object(codex_local_worker, "_git", fake_recovery_git),
              patch.object(
                  codex_local_worker.sb,
                  "checkpoint_personal_work_session_with_recovery",
                  return_value={"updated": True}),
              patch.object(
                  codex_local_worker.sb,
                  "complete_personal_claim_with_recovery",
                  return_value={"completed": True, "status": "In Review"}),
              patch.object(
                  codex_local_worker.sb,
                  "_cleanup_personal_bound_workspace",
                  side_effect=[{"cleaned": False, "reason": "retry"},
                               {"cleaned": True}])):
            restart_recovery_pending = (
                codex_local_worker.resume_pending_postprocessing())
            receipt_preserved_for_cleanup_retry = bool(
                list(recovery_root.glob("*.json")))
            shutil.rmtree(local_workspace)
            restart_recovery = codex_local_worker.resume_pending_postprocessing()
            local_workspace.mkdir()
            expired_path = recovery_root / "expired-recovery.json"
            recovery_receipt_template["recovery_deadline"] = time.time() - 1
            recovery_receipt_template["stage"] = "terminalized"
            codex_local_worker._atomic_recovery_json(
                expired_path, recovery_receipt_template)
            quarantine_scan = codex_local_worker.resume_pending_postprocessing()
            quarantine_repeat = codex_local_worker.resume_pending_postprocessing()
            quarantine_receipt = json.loads(expired_path.read_text(encoding="utf-8"))
            expired_path.unlink()
        local_recovered = local_lifecycle["fail"](
            "post_execution_validation_failed:test")
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
    completed_runner_index = next(
        index for index, (path, body) in enumerate(local_control_calls)
        if path == "/ixp/v1/register_runner_session" and body.get("status") == "completed")
    successful_wake_index = next(
        index for index, (path, body) in enumerate(local_control_calls)
        if path == "/txp/v1/complete_wake" and body["result"].get("started") is True)
    ok(central_binding_required
       and local_evidence["head_sha"] == completed_sha
       and local_evidence["verification"]["auth_mode"] == "chatgpt_personal"
       and local_evidence["verification"]["runner_heartbeat_errors_recovered"] == 1
       and local_evidence["verification"]["provider_credential_exported"] is False
       and "exec" in local_command and "ADAPTER-18" in str(local_command[-1])
       and "workspace-write" in local_command
       and "danger-full-access" not in local_command
       and local_command.count("--add-dir") == 1
       and str(local_git_common) in local_command and str(local_git_dir) not in local_command
       and "OPENAI_API_KEY" not in local_environment
       and "PM_MCP_TOKEN" not in local_environment
       and "SWITCHBOARD_TOKEN" not in local_environment
       and local_environment.get("CODEX_HOME") == str(linux_codex_home)
       and local_evidence["verification"]["host_coordination_credential_exported"] is False
       and any(path == "/ixp/v1/heartbeat_runner_session"
               for path, _body in local_control_calls)
       and any(path == "/txp/v1/complete_wake"
               and body["result"]["started"] is True
               for path, body in local_control_calls)
       and local_success_was_deferred
       and local_terminal.get("status") == "completed"
       and len(receipt_files_before_restart) == 1
       and restart_recovery_pending.get("pending_count") == 1
       and receipt_preserved_for_cleanup_retry
       and restart_recovery.get("recovered_count") == 1
       and restart_recovery.get("pending_count") == 0
       and quarantine_scan.get("pending_count") == 0
       and quarantine_scan.get("quarantined_count") == 1
       and quarantine_repeat.get("pending_count") == 0
       and quarantine_repeat.get("quarantined_count") == 1
       and quarantine_receipt.get("stage") == "recovery_quarantined"
       and quarantine_receipt.get("quarantine_reason")
           == "automatic_recovery_deadline_expired"
       and not list(recovery_root.glob("*.json"))
       and local_recovered.get("status") == "failed"
       and len([body for path, body in local_control_calls
                if path == "/txp/v1/complete_wake"
                and body["result"].get("started") is True]) == 2
       and len({json.dumps(body, sort_keys=True) for path, body in local_control_calls
                if path == "/txp/v1/complete_wake"
                and body["result"].get("started") is True}) == 1
       and any(path == "/txp/v1/complete_wake"
               and body["result"].get("recoverable_post_execution_failure") is True
               for path, body in local_control_calls)
       and completed_runner_index < successful_wake_index,
       "native local worker defers success and can recover a rejected post-execution gate")
    failed_runner_index = next(
        index for index, (path, body) in enumerate(failed_local_control_calls)
        if path == "/ixp/v1/register_runner_session" and body.get("status") == "failed")
    failed_wake_index = next(
        index for index, (path, _body) in enumerate(failed_local_control_calls)
        if path == "/txp/v1/complete_wake")
    ok(failed_local_visible and failed_runner_index < failed_wake_index
       and failed_local_control_calls[failed_wake_index][1]["result"]["started"] is False,
       "native execution failure terminalizes its exact runner before the failed wake receipt")
    original_personal_git = switchboard_core._personal_git
    original_checkpoint_http = switchboard_core._http
    checkpoint_payloads = []
    checkpoint_state = {"head": completed_sha, "status": ""}

    def checkpoint_git(args, **_kwargs):
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, checkpoint_state["head"] + "\n", "")
        if args[-2:] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, checkpoint_state["status"], "")
        raise AssertionError(args)

    def checkpoint_http(method, path, payload, **_kwargs):
        checkpoint_payloads.append((method, path, payload))
        return {"updated": True}

    try:
        switchboard_core._personal_git = checkpoint_git
        switchboard_core._http = checkpoint_http
        checkpoint = switchboard_core.checkpoint_personal_work_session(
            PROJECT, local_task["managed"], local_evidence,
            "codex/ADAPTER-18-local-worker")
        checkpoint_state["status"] = " M generated.py\n"
        try:
            switchboard_core.checkpoint_personal_work_session(
                PROJECT, local_task["managed"], local_evidence,
                "codex/ADAPTER-18-local-worker")
            dirty_checkpoint_denied = False
        except RuntimeError as exc:
            dirty_checkpoint_denied = "dirty after executed tests" in str(exc)
        checkpoint_state.update({"status": "", "head": "f" * 40})
        try:
            switchboard_core.checkpoint_personal_work_session(
                PROJECT, local_task["managed"], local_evidence,
                "codex/ADAPTER-18-local-worker")
            drift_checkpoint_denied = False
        except RuntimeError as exc:
            drift_checkpoint_denied = "HEAD drifted during executed tests" in str(exc)
    finally:
        switchboard_core._personal_git = original_personal_git
        switchboard_core._http = original_checkpoint_http
    clean_checkpoint_payload = checkpoint_payloads[0][2] if checkpoint_payloads else {}
    ok(checkpoint.get("updated") is True
       and clean_checkpoint_payload.get("head_sha") == completed_sha
       and clean_checkpoint_payload.get("dirty_status") == "clean"
       and dirty_checkpoint_denied and drift_checkpoint_denied,
       "personal checkpoint revalidates exact HEAD and cleanliness after executed tests")
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
                state_path=linux_state, project=PROJECT, http=http,
                service_runner=fake_service)
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
        project=PROJECT, http=http, service_runner=fake_service)
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
