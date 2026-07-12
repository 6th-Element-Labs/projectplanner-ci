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
ok("PM_RUNNER_DIR=/var/lib/projectplanner/runner" in service_text,
   "systemd Agent Host service keeps runner artifacts outside the git checkout")

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
ok("--ack-inbox" in cmd,
   "lane-less message wake explicitly acks adapter receipt")
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

# DELIVERABLES-23: closure_verification wakes are still lane-less/message-only by
# policy.mode (never a claim_next grab), but they run the deterministic closure
# engine instead of the old ack-only inbox stub.
closure_wake = {
    "wake_id": "wake-closure",
    "selector": {"runtime": "claude-code", "agent_id": "verifier/closure/some-deliverable"},
    "policy": {"mode": "message_only", "kind": "closure_verification",
              "deliverable_id": "some-deliverable", "gate_ids": ["scope"]},
}
ok(agent_host.wake_mode(closure_wake) == "closure_verify",
   "closure_verification wake selects closure_verify mode, not inbox_only")
ok(agent_host.eligible_runtime(closure_wake, message_only_inventory) is not None,
   "the safe-default message-only host is eligible for closure_verification wakes")
cmd, mode = agent_host.launch_command(closure_wake, message_only_inventory)
ok(mode == "closure_verify", "launch_command selects closure_verify mode for a closure wake")
ok(agent_host.CLOSURE_VERIFIER in cmd,
   "closure_verify command runs the deterministic verifier script")
ok("run_agent.py" not in " ".join(cmd),
   "closure_verify command does not fall through to the ack-only inbox stub")
ok("--deliverable-id" in cmd and "some-deliverable" in cmd,
   "closure_verify command carries the target deliverable id")
ok("--wake-id" in cmd and "wake-closure" in cmd,
   "closure_verify command carries the wake id for logging/correlation")

malformed_closure_wake = {
    "wake_id": "wake-closure-bad",
    "selector": {"runtime": "claude-code"},
    "policy": {"mode": "message_only", "kind": "closure_verification"},  # no deliverable_id
}
ok(agent_host.wake_mode(malformed_closure_wake) == "inbox_only",
   "a closure_verification wake missing deliverable_id falls back to the safe inbox-only stub")

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
runner_register_calls = [c for c in calls if c[1] == agent_host.P_REGISTER_RUNNER]
ok(summary["acted"] and summary["acted"][0]["wake_mode"] == "inbox_only",
   "run_once reports inbox-only mode for lane-less wake")
ok(complete_calls and complete_calls[0][2]["result"]["wake_mode"] == "inbox_only",
   "complete_wake records inbox-only mode")
ok(runner_register_calls and runner_register_calls[0][2]["runner_session_id"] == "run_test",
   "run_once registers launched runner session")

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

calls = []
runner_actions = []
kill_request = {
    "request_id": "runnerreq-kill",
    "runner_session_id": "run_test",
    "host_id": "host/test",
    "action": "kill",
    "options": {"grace_seconds": 0.1, "signal": "TERM"},
}


def fake_try_runner_control(method, path, body=None):
    calls.append((method, path, body or {}))
    if path.startswith(agent_host.P_LIST_RUNNER_CONTROLS):
        return {"requests": [kill_request]}
    if path == agent_host.P_CLAIM_RUNNER_CONTROL:
        return {"claimed": True, "request": kill_request}
    if path == agent_host.P_COMPLETE_RUNNER_CONTROL:
        return {"status": body.get("status")}
    if path.startswith(agent_host.P_LIST_WAKES):
        return {"wake_intents": []}
    return {"ok": True}


agent_host._try = fake_try_runner_control
agent_host.supervisor_action = lambda action, runner_session_id, options=None: runner_actions.append(
    (action, runner_session_id, options or {})) or {
        "status": "killed",
        "last_snapshot": {"runner_session_id": runner_session_id, "source": "test"},
    }
summary = agent_host.run_once(inventory)
control_claims = [c for c in calls if c[1] == agent_host.P_CLAIM_RUNNER_CONTROL]
control_completes = [c for c in calls if c[1] == agent_host.P_COMPLETE_RUNNER_CONTROL]
ok(runner_actions and runner_actions[0][0] == "kill",
   "run_once executes pending runner kill through supervisor")
ok(control_claims and control_claims[0][2]["request_id"] == "runnerreq-kill",
   "run_once claims runner control request for this host")
ok(control_completes and control_completes[0][2]["snapshot"]["runner_session_id"] == "run_test",
   "run_once completes runner control with snapshot")
ok(summary["runner_controls"] and summary["runner_controls"][0]["status"] == "completed",
   "run_once reports handled runner controls")

handshakes = []
inboxes = []
acks = []
sleeps = []

run_agent.sb.handshake = lambda project, agent_id, runtime, **kw: handshakes.append(
    (project, agent_id, runtime, kw))
run_agent.sb.inbox = lambda project, agent_id: inboxes.append((project, agent_id)) or [{"id": 1, "requires_ack": True}]
run_agent.sb.ack = lambda project, message_id, response="": acks.append(
    (project, message_id, response)) or {"acked": True}
run_agent.sb.claim_next = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("inbox_only must not call claim_next"))
run_agent.time.sleep = lambda seconds: sleeps.append(seconds)

rc = run_agent.inbox_only("claude/test", "claude-code", 0.25, ack_inbox=True)
ok(rc == 0, "run_agent inbox_only exits successfully")
ok(handshakes and handshakes[0][1] == "claude/test",
   "run_agent inbox_only registers the agent id")
ok(inboxes == [(run_agent.PROJECT, "claude/test")],
   "run_agent inbox_only reads unacked inbox")
ok(acks and acks[0][1] == 1 and "adapter" in acks[0][2],
   "run_agent inbox_only can ack adapter receipt without claiming work")
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
