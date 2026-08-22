#!/usr/bin/env python3
"""ADAPTER-60: first-class OpenCode Connect runtime with required Switchboard MCP."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from adapters.agent_host_enrollment import (
    SUPPORTED_HOST_RUNTIMES,
    preflight_opencode_local_auth,
    EnrollmentError,
)
from execution_policy_fixture import ready_execution_context
from switchboard.application.commands import connect_dispatch, execution_context
from switchboard.application.session_boot import ADVERTISED_LAUNCH_RUNTIMES
from switchboard.connect.execution_assignment import build_execution_assignment
from switchboard.domain.provider_credentials import (
    list_provider_auth_capabilities,
    provider_auth_decision,
)
from switchboard.storage.repositories.agent_host_enrollments import (
    _clamped_max_sessions,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def connect_wake(runtime="opencode", *, model="", project="switchboard"):
    wake = {
        "wake_id": "wake-opencode",
        "task_id": "ADAPTER-60",
        "_host_project": project,
        "selector": {
            "runtime": runtime,
            "task_id": "ADAPTER-60",
            "agent_id": f"agent/{runtime}/adapter-60",
        },
        "policy": {
            "mode": "connect",
            "assignment": {
                "schema": "switchboard.connect.assignment.v1",
                "assignment_id": "assignment-opencode",
                "principal_ref": f"agent/{runtime}/adapter-60",
                "work_ref": f"task:{project}:ADAPTER-60",
                "runtime": runtime,
                "provider": "opencode-zen",
                "workspace_ref": "repo:canonical",
                "queued_at": 1.0,
                "limits": {
                    "max_runtime_seconds": 7200,
                    "spend_limit_microunits": 0,
                    "memory_limit_bytes": 0,
                },
            },
            "lifecycle": {
                "schema": "switchboard.execution_lifecycle.v1",
                "role": "implementation",
                "head_sha": "",
                "pr_number": 0,
                "pr_url": "",
                "ttl_seconds": 7200,
                "execution_id": "execlease-opencode",
                "generation": 1,
                "fence_epoch": 1,
            },
        },
    }
    if model:
        wake["selector"]["model"] = model
        wake["policy"]["lifecycle"]["model"] = model
    wake["policy"]["execution_context"] = execution_context.with_generation(
        ready_execution_context("ADAPTER-60", runtime=runtime), 1)
    wake["policy"]["execution_assignment"] = build_execution_assignment(
        task_id=wake["task_id"],
        assignment=wake["policy"]["assignment"],
        lifecycle=wake["policy"]["lifecycle"],
        execution_context=wake["policy"]["execution_context"],
    )
    return wake


inventory = {
    "host_id": "host/opencode-test",
    "repo_root": str(ROOT),
    "policy": {"allow_global_claim": False, "allow_work": True,
               "lane_mode": "all_project_lanes"},
    "runtimes": [{
        "runtime": "opencode",
        "provider": "opencode-zen",
        "lanes": [],
        "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
    }],
}


def test_connect_admits_opencode_aliases():
    for requested in ("opencode", "opencode-cli", "zen"):
        runtime_name, provider = connect_dispatch._runtime(requested)
        ok(runtime_name == "opencode" and provider == "opencode-zen",
           f"{requested} maps to opencode / opencode-zen")
    try:
        connect_dispatch._runtime("cli")
    except ValueError as exc:
        ok(str(exc) == "unsupported_runtime", "unknown runtime still refuses")
    else:
        ok(False, "unknown runtime still refuses")
    ok("opencode" in ADVERTISED_LAUNCH_RUNTIMES,
       "launcher boot advertises opencode")


def test_execution_context_one_vendor():
    ok(execution_context._RUNTIME_ALIASES.get("opencode") == "opencode"
       and execution_context._RUNTIME_ALIASES.get("opencode-cli") == "opencode"
       and execution_context._RUNTIME_PROVIDERS.get("opencode") == "opencode-zen",
       "execution context binds opencode to opencode-zen only")


def test_provider_auth_matrix():
    matrix = list_provider_auth_capabilities()
    records = {row["capability_id"]: row for row in matrix["capabilities"]}
    host = records.get("opencode-zen-host-bound-native-cli") or {}
    api = records.get("opencode-zen-api-key-worker") or {}
    portable = records.get("opencode-zen-auth-json-portable-worker") or {}
    ok(host.get("provider") == "opencode-zen"
       and host.get("state") == "supported_host_bound"
       and host.get("host_class") == "user_owned_persistent"
       and host.get("litellm", {}).get("eligible") is False,
       "host-bound Zen login is supported_host_bound and not LiteLLM")
    ok(api.get("state") == "supported"
       and api.get("auth_mode") == "api_key"
       and api.get("litellm", {}).get("eligible") is False,
       "Zen API key is supported and stays on the native CLI")
    ok(portable.get("state") == "unavailable",
       "exporting host auth.json is unavailable")
    bound = provider_auth_decision(
        "opencode-zen", "zen_host_login", host_class="user_owned_persistent")
    unbound = provider_auth_decision("opencode-zen", "zen_host_login")
    ok(bound.get("allowed") is True and unbound.get("allowed") is False,
       "host-bound Zen login requires the persistent host class")


def test_connect_launch_argv_and_mcp_config():
    ok("opencode" in agent_host.CONNECT_RUNTIME_DEFAULTS
       and agent_host.CONNECT_RUNTIME_DEFAULTS["opencode"] == ("opencode", "--auto"),
       "Connect default is interactive opencode --auto")
    cmd, mode = agent_host.launch_command(
        connect_wake(), inventory, runner_session_id="run_opencode",
        workspace_path=str(ROOT))
    child = cmd[cmd.index("--") + 1:]
    ok(mode == "connect" and child[0] == "opencode" and "--auto" in child,
       f"Connect starts native OpenCode (argv={child[:4]})")
    ok("exec" not in child and "-p" not in child,
       "OpenCode Connect stays interactive")
    pin_cmd, _ = agent_host.launch_command(
        connect_wake(model="opencode/x-preview-f-free"), inventory,
        runner_session_id="run_ox_alpha", workspace_path=str(ROOT))
    pin_child = pin_cmd[pin_cmd.index("--") + 1:]
    ok("--model" in pin_child
       and pin_child[pin_child.index("--model") + 1] == "opencode/x-preview-f-free",
       "task model pin reaches --model")
    config = agent_host._connect_opencode_mcp_config("simplemark")
    mcp = ((config.get("mcp") or {}).get("taikun-plan") or {})
    encoded = json.dumps(config)
    ok(mcp.get("type") == "remote"
       and "mcp?project=simplemark" in str(mcp.get("url") or "")
       and mcp.get("oauth") is False
       and mcp.get("enabled") is True
       and "{env:SWITCHBOARD_CONNECT_SESSION_TOKEN}" in str(
           (mcp.get("headers") or {}).get("Authorization") or "")
       and "Bearer ey" not in encoded
       and "sk-" not in encoded,
       "session OpenCode config requires Switchboard MCP without embedding the token")
    env = agent_host._connect_opencode_session_env("switchboard")
    payload = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    ok(payload["mcp"]["taikun-plan"]["enabled"] is True,
       "OPENCODE_CONFIG_CONTENT carries the required MCP block")


def test_enrollment_preflight():
    ok("opencode" in SUPPORTED_HOST_RUNTIMES,
       "enrollment accepts --runtime opencode")

    try:
        preflight_opencode_local_auth(opencode_executable="/no/such/opencode")
        ok(False, "missing OpenCode CLI fails closed")
    except EnrollmentError:
        ok(True, "missing OpenCode CLI fails closed")

    with tempfile.TemporaryDirectory() as raw:
        dummy = Path(raw) / "opencode"
        dummy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dummy.chmod(0o755)

        def fake(command, **_kwargs):
            class Result:
                returncode = 0
                stdout = (
                    "1.0.0" if "--version" in command
                    else "opencode  zen  logged in\n")
                stderr = ""

            return Result()

        proof = preflight_opencode_local_auth(
            opencode_executable=str(dummy), runner=fake)
        ok(proof.get("authenticated") is True
           and proof.get("auth_mode") == "zen_host_login"
           and proof.get("credential_values_redacted") is True
           and str(proof.get("account_fingerprint") or "").startswith("acct-"),
           "Zen login preflight returns a redacted fingerprint")

        def logged_out(command, **_kwargs):
            class Result:
                returncode = 0
                stdout = "no credentials" if "auth" in command else "1.0.0"
                stderr = ""

            return Result()

        try:
            preflight_opencode_local_auth(
                opencode_executable=str(dummy), runner=logged_out)
            ok(False, "logged-out OpenCode preflight fails closed")
        except EnrollmentError:
            ok(True, "logged-out OpenCode preflight fails closed")


def test_opencode_host_uses_operator_concurrency_limit():
    ok(_clamped_max_sessions("opencode", 16) == 16,
       "OpenCode host honors an operator-set 16-session limit")
    ok(_clamped_max_sessions("opencode", 99) == 32,
       "OpenCode host still obeys the global personal-host ceiling")
    ok(_clamped_max_sessions("claude-code", 16) == 1,
       "Claude personal subscription keeps its exclusive-seat limit")


if __name__ == "__main__":
    test_connect_admits_opencode_aliases()
    test_execution_context_one_vendor()
    test_provider_auth_matrix()
    test_connect_launch_argv_and_mcp_config()
    test_enrollment_preflight()
    test_opencode_host_uses_operator_concurrency_limit()
    print(f"\nADAPTER-60 OpenCode runtime: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
