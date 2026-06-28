#!/usr/bin/env python3
"""Self-contained tests for the Agent Host wake consumer safety rules."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


agent_host = _load("agent_host", ROOT / "adapters" / "agent_host.py")
run_agent = _load("run_agent", ROOT / "adapters" / "run_agent.py")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


inventory = {
    "host_id": "host/test",
    "repo_root": str(ROOT),
    "limits": {"max_sessions": 2},
    "runtimes": [{
        "runtime": "claude-code",
        "lanes": ["ADAPTER"],
        "capabilities": ["docs", "python"],
    }],
}

message_wake = {
    "wake_id": "wake-message",
    "selector": {"runtime": "claude-code", "agent_id": "claude/test"},
    "policy": {},
}
lane_wake = {
    "wake_id": "wake-lane",
    "selector": {"runtime": "claude-code", "agent_id": "claude/test", "lane": "ADAPTER"},
    "policy": {},
}

cmd, mode = agent_host.launch_command(message_wake, inventory)
ok(mode == "inbox_only", "lane-less wake selects inbox-only mode")
ok("--inbox-only" in cmd and "--lanes" not in cmd,
   "lane-less wake command cannot enter claim_next")
ok("--idle-seconds" in cmd, "inbox-only command stays alive for readiness check")

cmd, mode = agent_host.launch_command(lane_wake, inventory)
ok(mode == "claim_next", "lane-scoped wake selects claim_next mode")
ok("--lanes" in cmd and "ADAPTER" in cmd and "--dry" in cmd,
   "lane-scoped dry wake enters claim_next with an explicit lane")
ok("--inbox-only" not in cmd, "lane-scoped wake does not use inbox-only mode")

explicit = dict(lane_wake)
explicit["policy"] = {"mode": "message_only"}
cmd, mode = agent_host.launch_command(explicit, inventory)
ok(mode == "inbox_only" and "--inbox-only" in cmd,
   "explicit message_only policy overrides lane claim loop")

calls = []


def fake_try(method, path, body=None):
    calls.append((method, path, body or {}))
    if path.startswith(agent_host.P_LIST_WAKES):
        return {"wake_intents": [message_wake]}
    if path == agent_host.P_CLAIM_WAKE:
        return {"claimed": True}
    if path == agent_host.P_COMPLETE_WAKE:
        return {"ok": True}
    return {"ok": True}


agent_host._try = fake_try
agent_host.active_session_count = lambda inv: 0
agent_host.launch = lambda wake, inv: {
    "runner_session_id": "run_test",
    "pid": 12345,
    "wake_mode": agent_host.wake_mode(wake),
}
agent_host.confirm_started = lambda rec: True
summary = agent_host.run_once(inventory)
complete_calls = [c for c in calls if c[1] == agent_host.P_COMPLETE_WAKE]
ok(summary["acted"] and summary["acted"][0]["wake_mode"] == "inbox_only",
   "run_once reports inbox-only mode for lane-less wake")
ok(complete_calls and complete_calls[0][2]["result"]["wake_mode"] == "inbox_only",
   "complete_wake records inbox-only mode")

handshakes = []
inboxes = []
sleeps = []

run_agent.sb.handshake = lambda project, agent_id, runtime, **kw: handshakes.append(
    (project, agent_id, runtime, kw))
run_agent.sb.inbox = lambda project, agent_id: inboxes.append((project, agent_id)) or [{"id": 1}]
run_agent.sb.claim_next = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("inbox_only must not call claim_next"))
run_agent.time.sleep = lambda seconds: sleeps.append(seconds)

rc = run_agent.inbox_only("claude/test", "claude-code", 0.25)
ok(rc == 0, "run_agent inbox_only exits successfully")
ok(handshakes and handshakes[0][1] == "claude/test",
   "run_agent inbox_only registers the agent id")
ok(inboxes == [(run_agent.PROJECT, "claude/test")],
   "run_agent inbox_only reads unacked inbox")
ok(sleeps == [0.25], "run_agent inbox_only idles for readiness checks")

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
