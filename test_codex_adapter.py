#!/usr/bin/env python3
"""Self-contained smoke tests for the Codex Switchboard adapter shim."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADAPTER = ROOT / "adapters" / "codex" / "codex_adapter.py"
spec = importlib.util.spec_from_file_location("codex_adapter", ADAPTER)
codex_adapter = importlib.util.module_from_spec(spec)
sys.modules["codex_adapter"] = codex_adapter
spec.loader.exec_module(codex_adapter)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    os.environ.pop("PM_AGENT_ID", None)
    aid = codex_adapter.codex_agent_id(str(ROOT))
    ok(aid.startswith("codex/"), "default agent id uses codex/<branch>")
    ok(not aid.startswith("claude/"), "default agent id never inherits Claude prefix")

    os.environ.pop("PM_CODEX_PRETOOL_MODE", None)
    t1 = codex_adapter.control_fidelity()
    ok(t1["tier"] == "T1" and t1["deny"] == "not_verified",
       "default fidelity is honest advisory T1")

    os.environ["PM_CODEX_PRETOOL_MODE"] = "deny"
    t2 = codex_adapter.control_fidelity()
    ok(t2["tier"] == "T2" and t2["deny"] == "adapter_cli_pre_tool",
       "deny mode advertises T2 only when explicitly enabled")

    name, ti, cwd = codex_adapter.normalize_pending({
        "toolCall": {
            "name": "mcp__taikun_plan__update_task",
            "arguments": "{\"status\":\"Done\"}",
        },
        "cwd": str(ROOT),
    })
    ok(name == "mcp__taikun_plan__update_task", "normalizes nested toolCall name")
    ok(ti == {"status": "Done"}, "normalizes JSON-string arguments")
    ok(cwd == str(ROOT), "preserves cwd")

    codex_adapter.sb._consume_interrupt = lambda *args, **kwargs: None
    codex_adapter.sb._lease_holder = lambda *args, **kwargs: None
    codex_adapter.sb._http = lambda *args, **kwargs: {
        "messages": [{"id": 42, "from_agent": "claude-code", "message": "hello"}]
    }
    inbox = codex_adapter.drain_inbox("codex/test")
    ok(inbox and inbox[0]["id"] == 42, "session-start drain reads unacked inbox")

    calls = []

    def fake_http(method, path, body=None, **kwargs):
        calls.append((method, path, body or {}))
        if path.endswith("/claim_next"):
            return {"claimed": False, "reason": "no_unblocked_work"}
        if path.endswith("/complete_claim"):
            return {"completed": True, "claim_id": body["claim_id"], "status": "In Review"}
        if path.endswith("/abandon_claim"):
            return {"abandoned": True, "claim_id": body["claim_id"]}
        return {"messages": []}

    codex_adapter.sb._http = fake_http
    claim = codex_adapter.claim_next(lanes="ADAPTER, PROTO", capabilities="python, docs")
    ok(claim["claimed"] is False, "claim-next returns scheduler response")
    ok(calls[-1][2]["lanes"] == ["ADAPTER", "PROTO"], "claim-next serializes lane list")
    ok(calls[-1][2]["capabilities"] == ["python", "docs"],
       "claim-next serializes capability list")

    complete = codex_adapter.complete_claim("taskclaim-test", {"head_sha": "abc"})
    ok(complete["status"] == "In Review", "complete calls TXP complete_claim")
    abandoned = codex_adapter.abandon_claim("taskclaim-test", "blocked")
    ok(abandoned["abandoned"] is True, "abandon calls TXP abandon_claim")

    verdict = codex_adapter.on_pre_tool({
        "toolCall": {
            "name": "mcp__taikun_plan__update_task",
            "arguments": {"status": "Done"},
        },
        "cwd": str(ROOT),
    })
    ok(verdict["decision"] == "deny", "self-Done update is denied through shared core")
    ok(verdict["agent_id"].startswith("codex/"), "pre-tool verdict carries Codex agent id")
finally:
    os.environ.pop("PM_CODEX_PRETOOL_MODE", None)

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
