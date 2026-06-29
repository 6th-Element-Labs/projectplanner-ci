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


service_text = (ROOT / "deploy" / "projectplanner-agent-host.service").read_text()
ok("PM_PROJECT=switchboard" in service_text,
   "systemd Agent Host service is scoped to the switchboard project")
ok("PM_HOST_LANES=__MESSAGE_ONLY__" in service_text,
   "systemd Agent Host service advertises message-only sentinel lane")
ok("PM_AGENT_HOST_ALLOW_WORK=0" in service_text,
   "systemd Agent Host service explicitly disables work claims")
ok("PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM=0" in service_text,
   "systemd Agent Host service explicitly forbids global claims")
ok("adapters/agent_host.py --interval 10" in service_text,
   "systemd Agent Host service runs the persistent daemon loop")
ok("PM_HOST_MAX_SESSIONS=1" in service_text,
   "systemd Agent Host service limits concurrent wake readers")

for key in ("PM_HOST_LANES", "PM_AGENT_HOST_ALLOW_WORK", "PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM"):
    os.environ.pop(key, None)
default_inventory = agent_host.default_inventory()
ok(default_inventory["policy"]["mode"] == "message_only",
   "default Agent Host policy is message-only")
ok(default_inventory["runtimes"][0]["lanes"] == [agent_host.MESSAGE_ONLY_LANE],
   "default Agent Host inventory fails closed with sentinel lane")

inventory = {
    "host_id": "host/test",
    "repo_root": str(ROOT),
    "policy": {
        "mode": "lane_scoped",
        "allow_message_only": True,
        "allow_work": True,
        "allow_global_claim": False,
        "allowed_lanes": ["ADAPTER"],
    },
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
global_claim_wake = {
    "wake_id": "wake-global",
    "selector": {"runtime": "claude-code", "agent_id": "claude/test"},
    "policy": {"mode": "claim_next"},
}
message_only_inventory = {
    "host_id": "host/message-only",
    "repo_root": str(ROOT),
    "policy": {
        "mode": "message_only",
        "allow_message_only": True,
        "allow_work": False,
        "allow_global_claim": False,
        "allowed_lanes": [agent_host.MESSAGE_ONLY_LANE],
    },
    "limits": {"max_sessions": 1},
    "runtimes": [{
        "runtime": "claude-code",
        "lanes": [agent_host.MESSAGE_ONLY_LANE],
        "capabilities": ["docs", "python"],
    }],
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
ok("--idle-seconds" in cmd, "lane-scoped dry wake stays alive for readiness check")
ok("--inbox-only" not in cmd, "lane-scoped wake does not use inbox-only mode")

explicit = dict(lane_wake)
explicit["policy"] = {"mode": "message_only"}
cmd, mode = agent_host.launch_command(explicit, inventory)
ok(mode == "inbox_only" and "--inbox-only" in cmd,
   "explicit message_only policy overrides lane claim loop")
ok(agent_host.wake_mode(global_claim_wake, inventory) == "refused",
   "lane-less claim_next wake is refused by default")
ok(agent_host.eligible_runtime(global_claim_wake, inventory) is None,
   "lane-less claim_next wake is not eligible without global policy")
try:
    agent_host.launch_command(global_claim_wake, inventory)
    refused_launch = False
except ValueError:
    refused_launch = True
ok(refused_launch, "launch_command refuses ineligible global claim wake")
ok(agent_host.eligible_runtime(lane_wake, message_only_inventory) is None,
   "message-only host refuses lane-scoped work wakes")

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

calls = []


def fake_try_lane(method, path, body=None):
    calls.append((method, path, body or {}))
    if path.startswith(agent_host.P_LIST_WAKES):
        return {"wake_intents": [global_claim_wake, lane_wake]}
    if path == agent_host.P_CLAIM_WAKE:
        return {"claimed": True}
    if path == agent_host.P_COMPLETE_WAKE:
        return {"ok": True}
    return {"ok": True}


agent_host._try = fake_try_lane
summary = agent_host.run_once(inventory)
claim_calls = [c for c in calls if c[1] == agent_host.P_CLAIM_WAKE]
ok(len(claim_calls) == 1 and claim_calls[0][2]["wake_id"] == "wake-lane",
   "run_once claims only the eligible lane-scoped wake")
ok(summary["acted"] and summary["acted"][0]["wake_mode"] == "claim_next",
   "run_once reports claim_next mode for lane-scoped wake")

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

sessions = []
run_agent.sb.run_session = lambda *a, **k: sessions.append((a, k)) or {
    "completed": [], "stopped": "no_unblocked_work"}
rc = run_agent.main(["--runtime", "claude-code", "--lanes", "ADAPTER",
                     "--max-tasks", "1", "--dry", "--idle-seconds", "0.5"])
ok(rc == 0, "run_agent claim mode exits successfully")
ok(sessions and sessions[0][1]["lanes"] == "ADAPTER",
   "run_agent claim mode passes explicit lane to claim_next loop")
ok(sleeps[-1:] == [0.5], "run_agent claim mode idles for readiness checks")

register_calls = []
loop_count = {"n": 0}


class StopLoop(Exception):
    pass


def flaky_register(method, path, body=None):
    if path == agent_host.P_REGISTER_HOST:
        register_calls.append((method, path, body or {}))
        return None
    return {"ok": True}


def stop_after_second_loop(inv):
    loop_count["n"] += 1
    if loop_count["n"] >= 2:
        raise StopLoop()
    return {"host_id": inv["host_id"], "pending": 0, "acted": []}


agent_host._try = flaky_register
agent_host.run_once = stop_after_second_loop
agent_host.time.sleep = lambda seconds: None
try:
    agent_host.run(interval=1)
except StopLoop:
    pass
ok(len(register_calls) >= 2,
   "Agent Host daemon retries registration after transient startup failure")

print(f"\n{passed} passed, {failed} failed")
if failed:
    raise SystemExit(1)
