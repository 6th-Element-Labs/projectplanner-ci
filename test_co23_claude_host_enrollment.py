#!/usr/bin/env python3
"""CO-23: host-bound claude-code Agent Host enrollment (subscription, operator Mac).

Proves the security invariants without a live host:
- the Claude local-auth preflight consumes the EXISTING host login only, never
  mints a token (`claude setup-token` is never invoked), never exports the
  credential, and fails closed on logged-out / API-key / Bedrock-Vertex /
  malformed auth;
- a claude-code service-run emits no Codex credential material (no CODEX_HOME,
  no PM_CODEX_EXECUTABLE), selects the claude-code runtime and work module,
  and refuses to start when the CLI is logged out;
- the server issues a claude-code personal execution policy with an exclusive
  one-seat concurrency bound that survives operator policy updates, and the
  registration inventory gate accepts exactly that runtime and no other.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from unittest.mock import patch

TMP = Path(tempfile.mkdtemp(prefix="co23-claude-host-enrollment-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_PROVIDER_VAULT_KEY"] = base64.urlsafe_b64encode(b"C" * 32).decode()
os.environ["PM_PROVIDER_VAULT_KEY_ID"] = "co23-host-test:v1"

import shutil  # noqa: E402

import store  # noqa: E402
from switchboard.storage.repositories import agent_host_enrollments as enrollment_store  # noqa: E402
from switchboard.storage.repositories import coordination as coordination_store  # noqa: E402
from adapters import agent_host_enrollment as enrollment  # noqa: E402

PROJECT = "switchboard"
store.init_db(PROJECT)
passed = failed = 0


def ok(condition, label):
    global passed, failed
    if condition:
        passed += 1
        print(f"PASS {label}")
    else:
        failed += 1
        print(f"FAIL {label}")


LOGGED_IN = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "email": "operator@example.com",
    "orgId": "11111111-2222-3333-4444-555555555555",
    "subscriptionType": "max",
}


def fake_claude(payload, *, version="2.1.202 (Claude Code)", exit_code=0):
    """Return a runner that emulates the native claude CLI without a subprocess."""
    calls = []

    def runner(command, **kwargs):
        calls.append({"command": list(command), "env": dict(kwargs.get("env") or {})})
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, version + "\n", "")
        if command[1:] == ["auth", "status", "--json"]:
            body = payload if isinstance(payload, str) else json.dumps(payload)
            return subprocess.CompletedProcess(command, exit_code, body + "\n", "")
        return subprocess.CompletedProcess(command, 1, "", "unexpected command")

    runner.calls = calls
    return runner


claude_binary = TMP / "bin" / "claude"
claude_binary.parent.mkdir(parents=True, exist_ok=True)
claude_binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
claude_binary.chmod(0o755)
claude_binary = claude_binary.resolve()

try:
    # ------------------------------------------------------------------
    # preflight_claude_local_auth
    # ------------------------------------------------------------------
    runner = fake_claude(LOGGED_IN)
    result = enrollment.preflight_claude_local_auth(
        claude_executable=str(claude_binary), runner=runner)
    ok(result.get("authenticated") is True
       and result.get("auth_mode") == "oauth_personal"
       and result.get("claude_executable") == str(claude_binary)
       and result.get("provider_credential_exported") is False
       and result.get("credential_values_redacted") is True,
       "claude preflight accepts the host claude.ai login as oauth_personal")

    serialized = json.dumps(result, sort_keys=True)
    ok("operator@example.com" not in serialized
       and "11111111-2222-3333-4444-555555555555" not in serialized
       and str(result.get("account_fingerprint") or "").startswith("acct-"),
       "claude preflight returns a redacted account fingerprint, never the account")

    commands = [" ".join(call["command"][1:]) for call in runner.calls]
    ok(commands == ["--version", "auth status --json"],
       "claude preflight runs exactly version+status and never mints a setup token")

    with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "metered-key-must-not-cross",
            "ANTHROPIC_AUTH_TOKEN": "token-must-not-cross",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-must-not-cross",
            "PM_MCP_TOKEN": "host-bearer-must-not-cross",
            "SWITCHBOARD_TOKEN": "alternate-bearer-must-not-cross",
    }):
        stripped_runner = fake_claude(LOGGED_IN)
        enrollment.preflight_claude_local_auth(
            claude_executable=str(claude_binary), runner=stripped_runner)
    ok(all(not (
        {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
         "PM_MCP_TOKEN", "SWITCHBOARD_TOKEN"} & set(call["env"]))
        for call in stripped_runner.calls),
       "claude preflight strips metered, token, and coordination credentials")

    versions_dir = TMP / "share" / "claude" / "versions"
    versions_dir.mkdir(parents=True)
    versioned_binary = versions_dir / "2.1.218"
    versioned_binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    versioned_binary.chmod(0o755)
    symlink_bin = TMP / "linkbin"
    symlink_bin.mkdir()
    claude_symlink = symlink_bin / "claude"
    claude_symlink.symlink_to(versioned_binary)
    symlinked = enrollment.preflight_claude_local_auth(
        claude_executable=str(claude_symlink), runner=fake_claude(LOGGED_IN))
    ok(symlinked.get("claude_executable") == str(claude_symlink),
       "preflight keeps the stable symlink path, not its version-named target")

    denied = []
    for label, payload in (
        ("logged-out", {**LOGGED_IN, "loggedIn": False}),
        ("api-key", {**LOGGED_IN, "authMethod": "api_key"}),
        ("bedrock", {**LOGGED_IN, "apiProvider": "bedrock"}),
        ("vertex", {**LOGGED_IN, "apiProvider": "vertex"}),
        ("malformed", "this is not json"),
        ("non-object", "[1, 2, 3]"),
    ):
        try:
            enrollment.preflight_claude_local_auth(
                claude_executable=str(claude_binary), runner=fake_claude(payload))
        except enrollment.EnrollmentError:
            denied.append(label)
    ok(denied == ["logged-out", "api-key", "bedrock", "vertex", "malformed", "non-object"],
       "claude preflight fails closed on every non-personal or ambiguous auth mode")

    try:
        enrollment.preflight_claude_local_auth(
            claude_executable=str(claude_binary),
            runner=fake_claude(LOGGED_IN, exit_code=1))
        status_exit_denied = False
    except enrollment.EnrollmentError:
        status_exit_denied = True
    ok(status_exit_denied,
       "claude preflight fails closed when auth status exits non-zero")

    # ------------------------------------------------------------------
    # service_run for a claude-code host
    # ------------------------------------------------------------------
    origin_repo = TMP / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=master",
                    str(origin_repo)], check=True, capture_output=True)
    source_repo = TMP / "source-checkout"
    subprocess.run(["git", "init", "--initial-branch=master", str(source_repo)],
                   check=True, capture_output=True)
    (source_repo / "README.md").write_text("co23\n", encoding="utf-8")
    git_env = {**os.environ,
               "GIT_AUTHOR_NAME": "co23", "GIT_AUTHOR_EMAIL": "co23@example.com",
               "GIT_COMMITTER_NAME": "co23", "GIT_COMMITTER_EMAIL": "co23@example.com"}
    subprocess.run(["git", "-C", str(source_repo), "add", "README.md"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source_repo), "commit", "-m", "co23"],
                   check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "-C", str(source_repo), "remote", "add", "origin",
                    str(origin_repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source_repo), "push", "origin", "master"],
                   check=True, capture_output=True)

    config_root = TMP / "config"
    config_root.mkdir(mode=0o700)
    identity_path = config_root / "identity.json"
    identity_path.write_text(json.dumps({
        "schema": "switchboard.agent_host_identity.v1",
        "host_id": "host/co23-claude",
        "enrollment_id": "hostenroll-co23",
        "identity_generation": 1,
        "public_key_fingerprint": "sha256:" + "a" * 64,
        "host_token": "aht-test-bearer",
    }), encoding="utf-8")
    identity_path.chmod(0o600)
    state_root = TMP / "state"
    (state_root / "runner").mkdir(parents=True)
    (state_root / "provider-runtimes").mkdir()
    repo_root = TMP / "release" / "current"
    (repo_root / "adapters").mkdir(parents=True)
    (repo_root / "adapters" / "agent_host.py").write_text("# stub\n", encoding="utf-8")

    claude_config = {
        "base_url": "https://plan.example.test",
        "project": PROJECT,
        "runtime": "claude-code",
        "work_module": "claude_personal_worker:run",
        "allow_work": True,
        "allow_global_claim": False,
        "lanes": [],
        "capabilities": ["docs", "github", "python", "tests"],
        "max_sessions": 1,
        "personal_wakes_only": False,
        "owner_user_id": "user-co23",
        "tenant_allowlist": [],
        "project_allowlist": [PROJECT],
        "provider_allowlist": ["anthropic-claude"],
        "claude_executable": str(claude_binary),
        "platform": "darwin",
        "service_path": str(TMP / "service.plist"),
        "repo_root": str(repo_root),
        "source_repo_root": str(source_repo),
        "work_source_root": str(source_repo),
        "runner_dir": str(state_root / "runner"),
        "runtime_root": str(state_root / "provider-runtimes"),
        "workspace_root": str(state_root / "workspaces"),
        "log_root": str(TMP / "logs"),
        "user_home": str(Path.home()),
        "agent_host_version": "0.0.1",
        "host_heartbeat_ttl_s": 180,
    }
    config_path = config_root / "config.json"
    config_path.write_text(json.dumps(claude_config), encoding="utf-8")
    config_path.chmod(0o600)

    captured = {}

    def capture_execve(executable, argv, env):
        captured.update({"executable": executable, "argv": list(argv), "env": dict(env)})

    logged_in_claude = TMP / "bin" / "claude-logged-in"
    logged_in_claude.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo '2.1.202 (Claude Code)'; exit 0; fi\n"
        "if [ \"$1\" = \"auth\" ] && [ \"$2\" = \"status\" ]; then "
        "printf '%s' '" + json.dumps(LOGGED_IN).replace("'", "") + "'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    logged_in_claude.chmod(0o755)
    # The service must keep the operator-maintained symlink path end to end:
    # the symlink target directory holds version-named binaries, so resolving
    # anywhere in the flow breaks `which claude` for the daemon.
    logged_in_link_dir = TMP / "bin-symlink"
    logged_in_link_dir.mkdir(exist_ok=True)
    logged_in_symlink = logged_in_link_dir / "claude"
    logged_in_symlink.symlink_to(logged_in_claude.resolve())
    logged_in_claude = logged_in_symlink
    claude_config["claude_executable"] = str(logged_in_claude)
    config_path.write_text(json.dumps(claude_config), encoding="utf-8")

    with patch.object(enrollment.os, "execve", capture_execve):
        enrollment.service_run(identity_path, config_path)
    env = captured.get("env") or {}
    ok(env.get("PM_RUNTIME") == "claude-code"
       and env.get("PM_AGENT_WORK_MODULE_CLAUDE_CODE") == "claude_personal_worker:run"
       and env.get("PM_CLAUDE_EXECUTABLE") == str(logged_in_claude)
       and env.get("PM_HOST_PROVIDERS") == "anthropic-claude"
       and env.get("PM_HOST_LOCAL_AUTH_MODE") == "oauth_personal"
       and env.get("PM_HOST_LOCAL_AUTH_AVAILABLE") == "1",
       "claude-code service-run selects the claude runtime, module, and local auth")
    ok("CODEX_HOME" not in env
       and not env.get("PM_CODEX_EXECUTABLE")
       and "PM_AGENT_HOST_CODEX_HOME" not in env
       and "PM_AGENT_HOST_SOURCE_CODEX_HOME" not in env
       and env.get("PM_AGENT_WORK_MODULE", "") == "",
       "claude-code service-run emits no Codex credential or module material")
    ok(str(logged_in_claude.parent) in (env.get("PATH") or "").split(os.pathsep),
       "claude-code service-run puts the claude executable directory on PATH")
    ok(env.get("PM_HOST_MAX_SESSIONS") == "1",
       "claude-code service-run keeps the exclusive one-seat session bound")

    captured.clear()
    logged_out_claude = TMP / "bin" / "claude-logged-out"
    logged_out_claude.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo '2.1.202 (Claude Code)'; exit 0; fi\n"
        "if [ \"$1\" = \"auth\" ]; then printf '%s' '{\"loggedIn\": false}'; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    logged_out_claude.chmod(0o755)
    logged_out_claude = logged_out_claude.resolve()
    claude_config["claude_executable"] = str(logged_out_claude)
    config_path.write_text(json.dumps(claude_config), encoding="utf-8")
    try:
        with patch.object(enrollment.os, "execve", capture_execve):
            enrollment.service_run(identity_path, config_path)
        logged_out_refused = False
    except enrollment.EnrollmentError:
        logged_out_refused = True
    ok(logged_out_refused and not captured,
       "claude-code service-run refuses a logged-out CLI before any daemon start")

    # ------------------------------------------------------------------
    # server: begin_agent_host_enrollment issues a claude-code policy
    # ------------------------------------------------------------------
    begun = enrollment_store.begin_agent_host_enrollment(
        owner_user_id="user-co23",
        requested_host_id="host/co23-claude",
        provider_allowlist=["anthropic-claude"],
        project=PROJECT,
    )
    policy = (begun.get("enrollment") or {}).get("execution_policy") or {}
    ok(policy.get("runtime") == "claude-code"
       and int(policy.get("max_sessions") or 0) == 1,
       "anthropic-claude enrollment issues a claude-code one-seat execution policy")

    codex_begun = enrollment_store.begin_agent_host_enrollment(
        owner_user_id="user-co23",
        project=PROJECT,
    )
    codex_policy = (codex_begun.get("enrollment") or {}).get("execution_policy") or {}
    ok(codex_policy.get("runtime") == "codex",
       "default enrollment still issues the codex execution policy")

    mixed = enrollment_store.begin_agent_host_enrollment(
        owner_user_id="user-co23",
        provider_allowlist=["anthropic-claude", "openai-codex"],
        project=PROJECT,
    )
    ok(mixed.get("error"),
       "mixed-provider enrollment is refused rather than guessing a runtime")

    # ------------------------------------------------------------------
    # server: inventory gate accepts exactly the enrolled runtime
    # ------------------------------------------------------------------
    identity = {
        "required": True,
        "owner_user_id": "user-co23",
        "tenant_allowlist": [],
        "project_allowlist": [PROJECT],
        "provider_allowlist": ["anthropic-claude"],
        "execution_policy": policy,
    }

    def inventory(runtime_name):
        local_auth = {
            "available": True,
            "runtime": runtime_name,
            "auth_mode": "oauth_personal",
            "account_fingerprint": "acct-" + "0" * 16,
            "credential_values_redacted": True,
            "provider_credential_exported": False,
        }
        capacity = {
            "owner": {
                "user_id": "user-co23",
                "tenant_allowlist": [],
                "project_allowlist": [PROJECT],
                "provider_allowlist": ["anthropic-claude"],
            },
            "local_auth": local_auth,
        }
        runtimes = [{
            "runtime": runtime_name,
            "lanes": [],
            "capabilities": ["docs", "github", "python", "tests"],
            "policy": {"allow_work": True, "allow_global_claim": False},
            "local_auth": local_auth,
        }]
        return capacity, runtimes

    capacity, runtimes = inventory("claude-code")
    accepted = coordination_store._enrollment_inventory_error(
        identity, capacity, PROJECT, runtimes=runtimes,
        limits={"max_sessions": 1})
    capacity_codex, runtimes_codex = inventory("codex")
    rejected = coordination_store._enrollment_inventory_error(
        identity, capacity_codex, PROJECT, runtimes=runtimes_codex,
        limits={"max_sessions": 1})
    ok(accepted is None,
       "registration gate admits a claude-code inventory for a claude-code policy")
    ok(isinstance(rejected, dict)
       and rejected.get("error_code") == "host_enrollment_policy_mismatch",
       "registration gate rejects a codex inventory against a claude-code policy")

    # ------------------------------------------------------------------
    # server: operator policy updates preserve runtime and the one-seat cap
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # install_host with runtime=claude-code
    # ------------------------------------------------------------------
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signing_key = Ed25519PrivateKey.generate()
    signing_private = TMP / "release-private.pem"
    signing_public = TMP / "release-public.pem"
    signing_private.write_bytes(signing_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    signing_public.write_bytes(signing_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    repo_checkout = Path(__file__).resolve().parent
    bundle_dir = TMP / "bundle-0.0.2"
    enrollment.create_signed_bundle(repo_checkout, bundle_dir, "0.0.2", signing_private)

    install_root = TMP / "install"
    install_paths = {
        "prefix": install_root / "releases-prefix",
        "config_root": install_root / "config",
        "state_root": install_root / "state",
        "workspace_root": install_root / "state" / "workspaces",
        "service_path": install_root / "service" / "co23.plist",
        "log_root": install_root / "logs",
        "source_repo_root": source_repo,
    }

    issued_policy = {
        "runtime": "claude-code",
        "lanes": [],
        "lane_mode": "all_project_lanes",
        "capabilities": ["docs", "github", "python", "tests"],
        "allow_work": True,
        "allow_global_claim": False,
        "max_sessions": 1,
        "local_auth_required": True,
        "personal_wakes_only": False,
    }

    def fake_http(method, url, payload, *token, **kwargs):
        if url.endswith("/ixp/v1/agent-host-enrollments/complete"):
            return {
                "enrollment": {
                    "enrollment_id": "hostenroll-co23-live",
                    "host_id": "host/co23-claude-live",
                    "principal_id": "host-co23-live",
                    "identity_generation": 1,
                    "owner_user_id": "user-co23",
                    "tenant_allowlist": [],
                    "project_allowlist": [PROJECT],
                    "provider_allowlist": ["anthropic-claude"],
                    "execution_policy": issued_policy,
                },
                "host_token": "aht-co23-live-bearer",
            }
        if url.endswith("/ixp/v1/agent-host-enrollments/finalize"):
            return {"finalized": True}
        raise AssertionError(f"unexpected enrollment HTTP call: {url}")

    def fake_service(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    installed = enrollment.install_host(
        bundle_dir=bundle_dir,
        public_key_path=signing_public,
        bootstrap_code="ahb-co23-bootstrap",
        base_url="https://plan.example.test",
        project=PROJECT,
        owner_user_id="user-co23",
        target_platform="darwin",
        paths=install_paths,
        runtime="claude-code",
        claude_executable=str(logged_in_claude),
        provider_allowlist=("anthropic-claude",),
        start_service=False,
        http=fake_http,
        service_runner=fake_service,
        local_auth_runner=fake_claude(LOGGED_IN),
    )
    installed_config = json.loads(
        (install_paths["config_root"] / "config.json").read_text())
    ok(installed.get("installed") is True
       and installed_config.get("runtime") == "claude-code"
       and installed_config.get("work_module") == "claude_personal_worker:run"
       and installed_config.get("provider_allowlist") == ["anthropic-claude"]
       and installed_config.get("claude_executable")
       and int(installed_config.get("max_sessions") or 0) == 1,
       "claude-code install writes a claude runtime config with the one-seat policy")
    ok(not installed_config.get("codex_home")
       and not installed_config.get("codex_executable")
       and not (install_paths["state_root"] / "codex-home").exists(),
       "claude-code install never touches or copies a Codex auth root")

    update_bundle = TMP / "bundle-0.0.3"
    enrollment.create_signed_bundle(repo_checkout, update_bundle, "0.0.3", signing_private)
    updated_install = enrollment.update_host(
        bundle_dir=update_bundle,
        public_key_path=signing_public,
        state_path=install_paths["state_root"] / "state.json",
        source_repo_root=source_repo,
        restart_service=False,
        service_runner=fake_service,
    )
    ok(updated_install.get("updated") is True
       and updated_install.get("version") == "0.0.3",
       "a claude-code install takes a signed update without a Codex auth root")

    mismatch_root = TMP / "install-mismatch"
    mismatch_paths = {
        "prefix": mismatch_root / "releases-prefix",
        "config_root": mismatch_root / "config",
        "state_root": mismatch_root / "state",
        "workspace_root": mismatch_root / "state" / "workspaces",
        "service_path": mismatch_root / "service" / "co23.plist",
        "log_root": mismatch_root / "logs",
        "source_repo_root": source_repo,
    }

    def mismatch_http(method, url, payload, *token, **kwargs):
        result = fake_http(method, url, payload, *token, **kwargs)
        if url.endswith("/complete"):
            result["enrollment"]["execution_policy"] = {
                **issued_policy, "runtime": "codex"}
        return result

    try:
        enrollment.install_host(
            bundle_dir=bundle_dir,
            public_key_path=signing_public,
            bootstrap_code="ahb-co23-mismatch",
            base_url="https://plan.example.test",
            project=PROJECT,
            owner_user_id="user-co23",
            target_platform="darwin",
            paths=mismatch_paths,
            runtime="claude-code",
            claude_executable=str(logged_in_claude),
            provider_allowlist=("anthropic-claude",),
            start_service=False,
            http=mismatch_http,
            service_runner=fake_service,
            local_auth_runner=fake_claude(LOGGED_IN),
        )
        runtime_mismatch_refused = False
    except enrollment.EnrollmentError:
        runtime_mismatch_refused = True
    ok(runtime_mismatch_refused,
       "install refuses a server policy whose runtime disagrees with the request")

    # ------------------------------------------------------------------
    # a second host instance gets its own launchd identity
    # ------------------------------------------------------------------
    import plistlib
    claude_service = TMP / "launchagents" / "com.6thelement.switchboard-agent-host-claude.plist"
    enrollment.render_service(
        "darwin",
        python="/usr/bin/python3",
        entrypoint=TMP / "release" / "current" / "adapters" / "agent_host_enrollment.py",
        identity_path=identity_path,
        config_path=config_path,
        service_path=claude_service,
        log_root=TMP / "logs-claude",
    )
    with claude_service.open("rb") as handle:
        claude_plist = plistlib.load(handle)
    ok(claude_plist.get("Label") == "com.6thelement.switchboard-agent-host-claude",
       "a suffixed service path renders its own launchd label, not the codex one")

    service_commands = []

    def capture_service(command, **kwargs):
        service_commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    enrollment.control_service("darwin", "stop", claude_service, runner=capture_service)
    ok(service_commands
       and service_commands[0][-1].endswith(
           "/com.6thelement.switchboard-agent-host-claude"),
       "service control targets the suffixed label so hosts never collide")

    from db.connection import _conn  # noqa: E402
    enrollment_id = (begun.get("enrollment") or {}).get("enrollment_id")
    with _conn(PROJECT) as connection:
        connection.execute(
            "UPDATE agent_host_enrollments SET status='active', host_id=? "
            "WHERE enrollment_id=?",
            ("host/co23-claude", enrollment_id),
        )
    updated = enrollment_store.update_agent_host_execution_policy(
        host_id="host/co23-claude",
        max_sessions=8,
        lane_mode="all_project_lanes",
        lane_allowlist=[],
        actor="co23-test",
        principal_id="principal-co23",
        project=PROJECT,
    )
    updated_policy = (updated.get("enrollment") or {}).get("execution_policy") or {}
    ok(updated_policy.get("runtime") == "claude-code"
       and int(updated_policy.get("max_sessions") or 0) == 1,
       "claude-code execution policy survives updates with the one-seat cap")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
