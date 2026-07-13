#!/usr/bin/env python3
"""ADAPTER-18 executable Claude Code cloud adapter/host proof."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from subprocess import CompletedProcess

from adapters.claude_cloud import (
    TOKEN_REF,
    ClaudeCloudAdapter,
    ReceiptStore,
    parse_cli_version,
    preflight_environment,
    session_receipt_from_output,
    validate_project_mcp_config,
)
from adapters.claude_cloud_host import (
    CAPABILITY,
    P_COMPLETE_WAKE,
    P_REGISTER_RUNNER,
    P_TALLY,
    build_dev_brief,
    eligible,
    inventory,
    process_wake,
    task_branch,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


ok(parse_cli_version("2.1.202 (Claude Code)") == (2, 1, 202),
   "Claude CLI semantic version is parsed")
url = "https://claude.ai/code/session_abc-123"
receipt = session_receipt_from_output(f"\x1b[32mCloud session:\x1b[0m {url}\r\n")
ok(receipt.get("ok") is True and receipt.get("session_id") == "cse_abc-123"
   and receipt.get("session_url") == url,
   "TTY output yields a stable provider ID and app-visible URL")
osc_receipt = session_receipt_from_output(
    f"\x1b]8;;{url}\x07Open cloud session\x1b]8;;\x07")
ok(osc_receipt.get("session_url") == url,
   "OSC-only terminal hyperlink still yields the provider receipt")
auth_failure = session_receipt_from_output(
    "Cloud sessions require a claude.ai login. Run /login to authenticate.", 1)
ok(auth_failure.get("error") == "claude_cloud_auth_required",
   "expired Claude subscription auth fails visibly")
tty_failure = session_receipt_from_output(
    "Error: --cloud requires an interactive terminal.", 1)
ok(tty_failure.get("error") == "claude_cloud_tty_required",
   "non-TTY launch fails visibly")


with tempfile.TemporaryDirectory(prefix="claude-cloud-config-") as temp:
    root = Path(temp)
    safe_config = {
        "mcpServers": {"taikun-plan": {
            "type": "http",
            "url": "https://plan.taikunai.com/mcp",
            "headers": {"Authorization": "Bearer ${SWITCHBOARD_TOKEN}"},
        }}
    }
    (root / ".mcp.json").write_text(json.dumps(safe_config), encoding="utf-8")
    ok(not validate_project_mcp_config(root),
       "provider-side MCP secret reference passes")
    safe_config["mcpServers"]["taikun-plan"]["headers"]["Authorization"] = (
        "Bearer " + "reusable-literal-credential-value"
    )
    (root / ".mcp.json").write_text(json.dumps(safe_config), encoding="utf-8")
    ok("project_mcp_config_contains_literal_bearer" in validate_project_mcp_config(root),
       "committed literal bearer credential is rejected")


dispatch = {
    "project": "switchboard",
    "task_id": "ADAPTER-18",
    "claim_id": "",
    "wake_id": "wake-cloud-18",
    "dev_brief": "Read ADAPTER-18, implement it, test, push, and open a PR.",
    "canonical_repo": "6th-Element-Labs/projectplanner",
    "branch": "claude/adapter-18-cloud",
    "active_sessions": 0,
    "mcp_access": {
        "endpoint": "https://plan.taikunai.com/mcp",
        "token_ref": TOKEN_REF,
        "scopes": ["read:task", "write:claim", "write:evidence"],
        "expires_at": 1783890000,
    },
}


with tempfile.TemporaryDirectory(prefix="claude-cloud-preflight-") as temp:
    root = Path(temp)
    (root / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"taikun-plan": {
            "type": "http", "url": "https://plan.taikunai.com/mcp",
            "headers": {"Authorization": "Bearer ${SWITCHBOARD_TOKEN}"},
        }}
    }), encoding="utf-8")

    def fake_run(command, cwd, timeout=15):
        key = tuple(command)
        outputs = {
            ("/fake/claude", "--version"): (0, "2.1.202 (Claude Code)\n", ""),
            ("/fake/claude", "auth", "status", "--json"): (
                0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                               "subscriptionType": "max"}), ""),
            ("git", "remote", "get-url", "origin"): (
                0, "git@github.com:6th-Element-Labs/projectplanner.git\n", ""),
            ("git", "branch", "--show-current"): (0, dispatch["branch"] + "\n", ""),
            ("git", "status", "--porcelain"): (0, "", ""),
            ("git", "rev-parse", "HEAD"): (0, "a" * 40 + "\n", ""),
            ("git", "ls-remote", "--exit-code", "--heads", "origin",
             "refs/heads/" + dispatch["branch"]): (0, "a" * 40 + "\tref\n", ""),
            ("git", "merge-base", "--is-ancestor", "origin/master", "HEAD"): (0, "", ""),
        }
        rc, stdout, stderr = outputs.get(key, (1, "", "unexpected command"))
        return CompletedProcess(command, rc, stdout, stderr)

    preflight = preflight_environment(
        dispatch, root, run=fake_run, claude_path="/fake/claude")
    ok(preflight.get("ok") is True and preflight.get("subscription_type") == "max",
       "preflight proves CLI, subscription auth, repo, exact push, and MCP config")

    def unauthenticated_run(command, cwd, timeout=15):
        result = fake_run(command, cwd, timeout)
        if command[1:] == ["auth", "status", "--json"]:
            return CompletedProcess(command, 1, json.dumps({"loggedIn": False,
                                                            "authMethod": "none"}), "")
        return result

    denied = preflight_environment(
        dispatch, root, run=unauthenticated_run, claude_path="/fake/claude")
    ok("claude_cloud_subscription_auth_required" in denied.get("errors", []),
       "preflight fails closed without claude.ai subscription auth")


class FakeLauncher:
    def __init__(self):
        self.calls = 0

    def launch(self, prompt, cwd):
        self.calls += 1
        return {"ok": True, "session_id": "cse_adapter18",
                "session_url": "https://claude.ai/code/session_adapter18",
                "status": "running", "output_hash": "sha256:" + "b" * 64}


with tempfile.TemporaryDirectory(prefix="claude-cloud-receipts-") as temp:
    launcher = FakeLauncher()
    adapter = ClaudeCloudAdapter(
        temp,
        launcher=launcher,
        receipt_store=ReceiptStore(Path(temp) / "receipts"),
        preflight_fn=lambda envelope, root: {"ok": True, "branch": envelope["branch"]},
    )
    adopted = adapter.trigger(dispatch)
    replayed = adapter.trigger(dispatch)
    ok(adopted.get("adopted") is True
       and adopted.get("runner_session_id") ==
       "cloud/claude-code-cloud/cse_adapter18",
       "complete Claude receipt is adopted and bound")
    ok(replayed.get("idempotent_replay") is True and launcher.calls == 1,
       "wake idempotency store prevents duplicate provider sessions")
    readback = adapter.get_session("cse_adapter18")
    ok(readback.get("status") == "running" and readback.get("session_url"),
       "host-local provider receipt supports status readback")


ok(task_branch("ADAPTER-18") == "claude/adapter-18-cloud",
   "provider branch is task-scoped and non-default")
try:
    task_branch("../master")
    unsafe_branch_rejected = False
except ValueError:
    unsafe_branch_rejected = True
ok(unsafe_branch_rejected, "unsafe provider branch input is rejected")
brief = build_dev_brief("ADAPTER-18", "claude/adapter-18-cloud")
ok("SWITCHBOARD_TOKEN" not in brief and "Never switch to" in brief,
   "dev brief contains branch safety but no credential reference")
ui_source = (Path(__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
ok("Open Claude session" in ui_source and "d.session_url" in ui_source,
   "Dev tab exposes an app-visible Claude session action")

inv = inventory()
inv["host_id"] = "host/test-cloud"
inv["repo_root"] = "/repo"
inv["runtimes"][0]["lanes"] = ["ADAPTER"]
wake = {
    "wake_id": "wake-host-18",
    "task_id": "ADAPTER-18",
    "selector": {"runtime": "claude-code", "lane": "ADAPTER",
                 "capabilities": [CAPABILITY]},
    "policy": {"mode": "vendor_cloud"},
}
ok(eligible(wake, inv), "cloud host accepts explicit vendor_cloud wake")
local_wake = {**wake, "selector": {"runtime": "claude-code", "lane": "ADAPTER",
                                    "capabilities": ["python"]}}
ok(not eligible(local_wake, inv), "cloud host refuses local-compute wake")


class FakeAdapter:
    def __init__(self, root):
        self.root = root

    def trigger(self, envelope):
        return {
            "ok": True,
            "adopted": True,
            "provider_session_id": "cse_host18",
            "session_url": "https://claude.ai/code/session_host18",
            "runner_session_id": "cloud/claude-code-cloud/cse_host18",
        }


calls = []


def fake_call(method, path, body=None):
    calls.append((method, path, body or {}))
    if path == "/txp/v1/claim_wake":
        return {"claimed": True}
    if path.startswith("/ixp/v1/runner_sessions"):
        return {"sessions": []}
    return {"ok": True}


with tempfile.TemporaryDirectory(prefix="claude-cloud-host-") as temp:
    clone = Path(temp) / "clone"
    clone.mkdir()
    result = process_wake(
        wake,
        inv,
        call=fake_call,
        adapter_factory=FakeAdapter,
        prepare=lambda repo, task, wake_id: (
            clone, "claude/adapter-18-cloud", "c" * 40),
    )
    ok(result.get("started") is True and result.get("session_url"),
       "cloud host starts and returns the provider binding")
    runner_body = next(body for method, path, body in calls if path == P_REGISTER_RUNNER)
    complete_body = next(body for method, path, body in calls if path == P_COMPLETE_WAKE)
    tally_body = next(body for method, path, body in calls if path == P_TALLY)
    ok((runner_body.get("metadata") or {}).get("session_url") == result["session_url"]
       and complete_body.get("runner_session_id") == result["runner_session_id"],
       "session URL is bound to runner and wake receipts")
    complete_result = complete_body.get("result") or {}
    ok(complete_result.get("heartbeat_ttl_s") == 86400
       and complete_result.get("billing_mode") == "subscription"
       and complete_result.get("cwd") == str(clone),
       "wake completion preserves the hosted runner lifetime and billing metadata")
    ok(tally_body.get("confidence") == "unknown" and tally_body.get("cost_usd") == 0
       and tally_body.get("request_id") == "claude-cloud:wake-host-18",
       "subscription use is recorded honestly and idempotently in Tally")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
