#!/usr/bin/env python3
"""BUG-126: a Connect launch opens Watch/Chat before wake completion."""

import importlib.util
import sys

from path_setup import ROOT

spec = importlib.util.spec_from_file_location(
    "bug126_agent_host", ROOT / "adapters" / "agent_host.py")
agent_host = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent_host
spec.loader.exec_module(agent_host)


events = []
binding = {
    "runner_session_id": "run_bug126",
    "task_id": "BUG-126",
    "claim_id": "taskclaim-bug126",
    "work_session_id": "worksession-bug126",
    "host_id": "host/test",
}


agent_host.wait_for_runner_binding = lambda *args, **kwargs: {
    "bound": True,
    "reason": "runner_bound",
    "session": {
        "runner_session_id": "run_bug126",
        "task_id": "BUG-126",
        "claim_id": "taskclaim-bug126",
        "status": "running",
        "metadata": {"work_session_id": "worksession-bug126"},
    },
}
agent_host._missing_local_runner_transport = lambda rec: []
agent_host.register_runner_session = lambda rec, wake, inventory: {
    **rec,
    "claim_id": "taskclaim-bug126",
    "metadata": {
        **dict(rec.get("metadata") or {}),
        "work_session_id": "worksession-bug126",
    },
    "server_relay": {
        "host_url": "wss://plan.example/pty/host?ticket=bug126",
        "binding": binding,
    },
}


def ensure_bridge(**kwargs):
    events.append(("bridge", kwargs))


def request(method, path, body=None):
    events.append(("request", path, body or {}))
    return {"ok": True}


agent_host._ensure_host_bridge = ensure_bridge
agent_host._try = request

wake = {
    "wake_id": "wake-bug126",
    "task_id": "BUG-126",
    "selector": {"agent_id": "codex/bug126", "runtime": "codex"},
    "policy": {"mode": "connect", "require_runner_bind": True},
}
inventory = {"host_id": "host/test", "repo_root": str(ROOT)}
record = {
    "runner_session_id": "run_bug126",
    "wake_mode": "connect",
    "task_id": "BUG-126",
    "pid": 4242,
    "log_path": "/tmp/run_bug126/stdout.log",
    "pty": True,
    "stream_bind": "127.0.0.1",
    "stream_port": 43210,
}

result = agent_host._finalize_bound_runner(
    wake, inventory, "run_bug126", record)

bridge_index = next(i for i, event in enumerate(events) if event[0] == "bridge")
complete_index = next(
    i for i, event in enumerate(events)
    if event[0] == "request" and event[1] == agent_host.P_COMPLETE_WAKE)
bridge_call = events[bridge_index][1]

assert bridge_index < complete_index, events
assert bridge_call["runner_session_id"] == "run_bug126", bridge_call
assert bridge_call["host_id"] == "host/test", bridge_call
assert bridge_call["binding"] == binding, bridge_call
assert bridge_call["host_relay_url"].startswith("wss://plan.example/pty/host"), bridge_call
assert bridge_call["child_pid"] == 4242, bridge_call
assert bridge_call["log_path"] == "/tmp/run_bug126/stdout.log", bridge_call
assert result["started"] is True, result
assert result["wake_completed"] is True, result
assert result["host_relay_error"] is None, result

agent_host.register_runner_session = lambda rec, wake, inventory: {
    **rec,
    "claim_id": "taskclaim-bug126",
    "metadata": {
        **dict(rec.get("metadata") or {}),
        "work_session_id": "worksession-bug126",
    },
}
events.clear()
missing_relay = agent_host._finalize_bound_runner(
    wake, inventory, "run_bug126", record)

assert missing_relay["started"] is True, missing_relay
assert missing_relay["host_relay_error"] == "missing_host_url", missing_relay
assert not any(event[0] == "bridge" for event in events), events

print("PASS: Connect opens the bound host bridge before completing the wake")
print("PASS: Connect names a missing launch relay instead of hiding it")
