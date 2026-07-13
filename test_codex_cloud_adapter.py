#!/usr/bin/env python3
"""ADAPTER-19: Codex cloud CLI trigger, receipt, readback, and failure contract."""

from __future__ import annotations

import json
import subprocess

from adapters.codex.cloud_adapter import (
    CodexCloudAdapter,
    build_cloud_prompt,
    launch_wake,
    usage_receipt,
)
from adapters.cloud_execution import CANONICAL_REPO, validate_usage_receipt


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class FakeRunner:
    def __init__(self, *, environment_error=""):
        self.calls = []
        self.created = False
        self.environment_error = environment_error

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(args, 0, "git@github.com:6th-Element-Labs/projectplanner.git\n", "")
        if args[-1:] == ["--version"]:
            return subprocess.CompletedProcess(args, 0, "codex-cli 0.144.0-alpha.4\n", "")
        if "cloud" in args and "list" in args:
            if self.environment_error:
                return subprocess.CompletedProcess(args, 1, "", self.environment_error)
            tasks = []
            if self.created:
                tasks = [{
                    "id": "task_i_adapter19",
                    "url": "https://chatgpt.com/codex/tasks/task_i_adapter19",
                    "title": "ADAPTER-19",
                    "status": "pending",
                    "updated_at": "2026-07-13T00:00:00Z",
                    "environment_id": "env-projectplanner",
                    "environment_label": "projectplanner",
                    "summary": {"files_changed": 0, "lines_added": 0, "lines_removed": 0},
                }]
            return subprocess.CompletedProcess(args, 0, json.dumps({"tasks": tasks, "cursor": None}), "")
        if "cloud" in args and "exec" in args:
            self.created = True
            return subprocess.CompletedProcess(
                args, 0, "https://chatgpt.com/codex/tasks/task_i_adapter19\n", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected command")


DISPATCH = {
    "schema": "switchboard.cloud_dispatch.v1",
    "project": "switchboard",
    "task_id": "ADAPTER-19",
    "claim_id": "claim-19",
    "wake_id": "wake-19",
    "dev_brief": "Implement ADAPTER-19, test it, and open a PR.",
    "canonical_repo": CANONICAL_REPO,
    "branch": "codex/adapter-19-codex-cloud",
    "mcp_access": {
        "endpoint": "https://plan.taikunai.com/mcp",
        "token_ref": "switchboard://scoped-token/ADAPTER-19",
        "scopes": ["read:task", "write:claim", "write:evidence"],
        "expires_at": 1783900000,
    },
}


missing_env = CodexCloudAdapter(
    cli_path="python3", repo_path=".", mcp_environment_bridge=True,
    agent_internet_enabled=True, runner=FakeRunner())
denied = missing_env.preflight(DISPATCH)
ok(denied["reason"] == "missing_provider_setup"
   and "codex_cloud_environment_id" in denied["missing"],
   "missing Codex cloud environment fails before provider launch")

missing_mcp = CodexCloudAdapter(
    environment_id="env-projectplanner", cli_path="python3", repo_path=".",
    mcp_environment_bridge=False, agent_internet_enabled=True, runner=FakeRunner())
denied = missing_mcp.preflight(DISPATCH)
ok("scoped_mcp_environment_bridge" in denied["missing"],
   "missing scoped MCP bridge fails closed instead of putting a token in the prompt")

fake = FakeRunner()
adapter = CodexCloudAdapter(
    environment_id="env-projectplanner",
    cli_path="python3",
    repo_path=".",
    mcp_environment_bridge=True,
    agent_internet_enabled=True,
    runner=fake,
    sleep=lambda _: None,
)
preflight = adapter.preflight(DISPATCH)
ok(preflight.get("allowed") is True and preflight["environment_id"] == "env-projectplanner",
   "preflight proves CLI auth/environment and canonical local repository")

binding = adapter.launch(DISPATCH, active_sessions=0)
ok(binding.get("adopted") is True and binding.get("provider_session_id") == "task_i_adapter19",
   "codex cloud exec receipt is read back and adopted")
ok(binding.get("session_url") == "https://chatgpt.com/codex/tasks/task_i_adapter19"
   and binding.get("runner_session_id", "").endswith("/task_i_adapter19"),
   "app-visible Codex URL binds to the runner-session identity")

exec_call = next(call for call in fake.calls if "exec" in call)
ok("--env" in exec_call and "env-projectplanner" in exec_call
   and "--branch" in exec_call and DISPATCH["branch"] in exec_call,
   "CLI launch pins environment and non-default task branch")
prompt = exec_call[-1]
ok(DISPATCH["mcp_access"]["token_ref"] in prompt
   and "raw-secret-value" not in prompt,
   "prompt carries only the opaque MCP credential reference")
ok("never main or master" in build_cloud_prompt(DISPATCH),
   "provider prompt preserves the branch safety invariant")

receipt = usage_receipt(binding)
ok(not validate_usage_receipt(receipt) and receipt["cost_usd"] == 0
   and receipt["confidence"] == "unknown",
   "subscription usage is recorded as zero/unknown, never fabricated")

wake_fake = FakeRunner()
wake_adapter = CodexCloudAdapter(
    environment_id="env-projectplanner", cli_path="python3", repo_path=".",
    mcp_environment_bridge=True, agent_internet_enabled=True,
    runner=wake_fake, sleep=lambda _: None)
wake = {
    "project": "switchboard",
    "wake_id": "wake-19",
    "task_id": "ADAPTER-19",
    "reason": "Implement ADAPTER-19",
    "selector": {"runtime": "codex", "agent_id": "codex/ADAPTER-19", "lane": "ADAPTER"},
    "policy": {
        "mode": "cloud_execution",
        "cloud_execution": {
            "branch": DISPATCH["branch"],
            "mcp_access": DISPATCH["mcp_access"],
        },
    },
}
record = launch_wake(
    wake, {"project": "switchboard", "repo_root": ".", "host_id": "host/codex-cloud"},
    active_sessions=0, adapter=wake_adapter)
ok(record.get("started") is True and record.get("cloud_session") is True
   and record.get("metadata", {}).get("session_url") == record.get("session_url"),
   "wake launch returns runner-session metadata with the app-visible URL")
ok(record.get("control", {}).get("runner_kill") is False
   and record.get("control", {}).get("vendor_managed") is True,
   "vendor-managed cloud task does not advertise a fake local kill handle")

repo_denied = CodexCloudAdapter(
    environment_id="env-projectplanner", cli_path="python3", repo_path=".",
    mcp_environment_bridge=True, agent_internet_enabled=True,
    runner=FakeRunner(environment_error="Error: repository is not accessible"))
failure = repo_denied.preflight(DISPATCH)
ok(failure.get("failure_class") == "absent_permission"
   and "github_repo_grant" in failure.get("missing", []),
   "Codex repo/environment access failure remains explicit and actionable")

from adapters import agent_host  # noqa: E402

ok(agent_host.wake_mode(wake, {}) == "cloud_execution",
   "Agent Host routes explicit Codex cloud wakes away from the local supervisor")
old_count = agent_host.active_codex_cloud_session_count
old_launch = agent_host.launch_codex_cloud_wake
try:
    agent_host.active_codex_cloud_session_count = lambda: 0
    agent_host.launch_codex_cloud_wake = lambda w, i, active_sessions=0: {
        "started": True,
        "cloud_session": True,
        "wake_mode": "cloud_execution",
        "runner_session_id": "cloud/openai-codex-cloud/task_i_adapter19",
        "provider_session_id": "task_i_adapter19",
        "session_url": "https://chatgpt.com/codex/tasks/task_i_adapter19",
    }
    host_record = agent_host.launch(wake, {
        "project": "switchboard", "host_id": "host/codex-cloud", "repo_root": ".",
        "policy": {"allow_work": True},
        "runtimes": [{"runtime": "codex", "policy": {"allow_work": True},
                      "lanes": ["ADAPTER"], "capabilities": ["cloud_execution"]}],
    })
    ok(host_record.get("cloud_session") is True
       and host_record.get("host_id") == "host/codex-cloud"
       and agent_host.confirm_started(host_record),
       "Agent Host launch path accepts only a complete Codex cloud binding receipt")
finally:
    agent_host.active_codex_cloud_session_count = old_count
    agent_host.launch_codex_cloud_wake = old_launch

print(f"\nCodex cloud adapter: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
