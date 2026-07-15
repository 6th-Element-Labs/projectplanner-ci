#!/usr/bin/env python3
"""Self-contained tests for the Agent Host wake consumer safety rules."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
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
os.environ["PM_RUNTIME"] = "codex"
os.environ["PM_HOST_ENROLLMENT_ID"] = "hostenroll-test"
os.environ["PM_HOST_IDENTITY_GENERATION"] = "3"
os.environ["PM_HOST_PUBLIC_KEY_FINGERPRINT"] = "sha256:" + "a" * 64
os.environ["PM_HOST_OWNER_USER_ID"] = "user-test"
os.environ["PM_HOST_TENANTS"] = "tenant-test"
os.environ["PM_HOST_PROJECTS"] = "switchboard"
os.environ["PM_HOST_PROVIDERS"] = "openai-codex"
os.environ["PM_HOST_LOCAL_AUTH_AVAILABLE"] = "1"
os.environ["PM_HOST_LOCAL_AUTH_MODE"] = "chatgpt_personal"
os.environ["PM_HOST_LOCAL_AUTH_ACCOUNT_PROOF"] = "raw-proof-never-exported"
os.environ["PM_MCP_TOKEN"] = "secret-host-token-never-exported"
default_inventory = agent_host.default_inventory()
ok(default_inventory["policy"]["mode"] == "message_only",
   "default Agent Host policy is message-only")
ok(default_inventory["runtimes"][0]["lanes"] == [agent_host.MESSAGE_ONLY_LANE],
   "default Agent Host inventory fails closed with sentinel lane")
default_inventory_text = json.dumps(default_inventory, sort_keys=True)
ok(default_inventory["agent_host_version"] == "0.2.0"
   and default_inventory["capacity"]["identity"]["identity_generation"] == 3,
   "enrolled Agent Host publishes versioned redacted identity proof")
ok(default_inventory["capacity"]["owner"]["tenant_allowlist"] == ["tenant-test"]
   and default_inventory["capacity"]["local_auth"]["auth_mode"] == "chatgpt_personal",
   "inventory advertises owner allowlists and local-auth availability")
ok("raw-proof-never-exported" not in default_inventory_text
   and "secret-host-token-never-exported" not in default_inventory_text
   and default_inventory["capacity"]["local_auth"]["credential_values_redacted"] is True,
   "host inventory cannot expose local account proof or bearer material")

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
incomplete_personal_wake = {
    "wake_id": "wake-personal-incomplete",
    "task_id": "ADAPTER-18",
    "selector": {"runtime": "codex", "agent_id": "codex/ADAPTER-18", "lane": "ADAPTER"},
    "policy": {"execution_mode": "personal_agent_host", "account_binding": {
        "task_id": "ADAPTER-18", "claim_id": "taskclaim-test",
        "work_session_id": "worksession-test", "runner_session_id": "runner-test",
        "host_id": "host/test", "agent_id": "codex/ADAPTER-18",
    }},
}
personal_runner_id = agent_host._runner_session_id_for_wake(
    incomplete_personal_wake, "host/test")
incomplete_personal_wake["policy"].update({
    "source_sha": "a" * 40,
    "execution_connection_id": "execconn-test",
})
incomplete_personal_wake["policy"]["account_binding"][
    "runner_session_id"] = personal_runner_id
complete_personal_wake = json.loads(json.dumps(incomplete_personal_wake))
complete_personal_wake["policy"]["execution_binding"] = {
    "wake_id": "wake-personal-incomplete", "task_id": "ADAPTER-18",
    "claim_id": "taskclaim-test", "work_session_id": "worksession-test",
    "runner_session_id": personal_runner_id,
    "host_id": "host/test",
    "source_sha": "a" * 40,
    "execution_connection_id": "execconn-test",
    "agent_id": "codex/ADAPTER-18",
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

incomplete_binding = agent_host.validate_personal_wake_binding(
    incomplete_personal_wake, inventory)
complete_binding = agent_host.validate_personal_wake_binding(
    complete_personal_wake, inventory)
ok(incomplete_binding["valid"] is False
   and incomplete_binding["error"] == "wake_binding_incomplete"
   and "source_sha[1]" in incomplete_binding["missing"]
   and "execution_connection_id[1]" in incomplete_binding["missing"],
   "personal host wake refuses incomplete source/connection binding before claim")
ok(complete_binding["valid"] is True and complete_binding["binding"]["task_id"] == "ADAPTER-18",
   "personal host wake accepts exact task/claim/session/host/source/connection binding")
inconsistent_personal_wake = json.loads(json.dumps(complete_personal_wake))
inconsistent_personal_wake["policy"]["execution_binding"]["claim_id"] = "taskclaim-other"
inconsistent_personal_wake["policy"]["execution_binding"]["source_sha"] = "not-a-sha"
inconsistent_binding = agent_host.validate_personal_wake_binding(
    inconsistent_personal_wake, inventory)
ok(inconsistent_binding["valid"] is False
   and "claim_id" in inconsistent_binding["mismatches"]
   and "source_sha" in inconsistent_binding["malformed"],
   "personal host wake refuses relationally inconsistent or malformed exact bindings")

personal_inventory = json.loads(json.dumps(inventory))
personal_inventory["runtimes"][0]["runtime"] = "codex"
personal_calls = []


def fake_try_personal(method, path, body=None):
    personal_calls.append((method, path, body or {}))
    if path.startswith(agent_host.P_LIST_WAKES):
        return {"wake_intents": [incomplete_personal_wake, complete_personal_wake]}
    if path == agent_host.P_CLAIM_WAKE:
        return {"claimed": True, "wake": complete_personal_wake}
    return {"ok": True}


personal_launch_envs = []
original_preclaim_registration = agent_host._register_preclaim_runner
agent_host._try = fake_try_personal
agent_host.active_session_count = lambda inv: 0
agent_host._register_preclaim_runner = lambda wake, inv, runner_id: {"ok": True}
agent_host.launch = lambda wake, inv, runner_session_id="", extra_env=None: (
    personal_launch_envs.append(dict(extra_env or {})) or {
        "runner_session_id": runner_session_id,
        "pid": 12344,
        "wake_mode": agent_host.wake_mode(wake),
    })
agent_host.confirm_started = lambda rec: True
personal_summary = agent_host.run_once(personal_inventory)
agent_host._register_preclaim_runner = original_preclaim_registration
personal_claims = [call for call in personal_calls if call[1] == agent_host.P_CLAIM_WAKE]
ok(len(personal_claims) == 1
   and personal_claims[0][2]["wake_id"] == complete_personal_wake["wake_id"]
   and personal_summary["refused"][0]["wake_id"] == incomplete_personal_wake["wake_id"],
   "daemon refuses an unbound personal wake and claims only the exact-bound wake")
ok(bool(personal_launch_envs)
   and personal_launch_envs[0]["PM_SOURCE_SHA"] == "a" * 40
   and personal_launch_envs[0]["PM_EXECUTION_CONNECTION_ID"] == "execconn-test"
   and personal_launch_envs[0]["PM_PERSONAL_AGENT_HOST_EXECUTION"] == "1"
   and personal_launch_envs[0]["PM_WORK_SESSION_ID"] == "worksession-test"
   and personal_launch_envs[0]["PM_CLAIM_ID"] == "taskclaim-test",
   "native launch receives the exact source SHA and execution-connection binding")

cmd, mode = agent_host.launch_command(message_wake, inventory)
ok(mode == "inbox_only", "lane-less wake selects inbox-only mode")
ok("--inbox-only" in cmd and "--lanes" not in cmd,
   "lane-less wake command cannot enter claim_next")
ok("--ack-inbox" in cmd,
   "lane-less message wake explicitly acks adapter receipt")
ok("--idle-seconds" in cmd, "inbox-only command stays alive for readiness check")

cmd, mode = agent_host.launch_command(lane_wake, inventory)
ok(mode == "claim_next", "lane-scoped wake selects claim_next mode")
separator = cmd.index("--")
ok(cmd[0] == agent_host.sys.executable
   and cmd[separator + 1] == agent_host.sys.executable,
   "supervisor and delegated worker inherit the Agent Host virtualenv interpreter")
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

# confirm_closure_verified: closure_verify jobs are deterministic and routinely exit
# well within the liveness grace window on success, unlike a long-lived LLM session
# (a fast exit there usually means it crashed). Bare process-liveness would wrongly
# call that launch_failed, so a finished job's own last-line JSON verdict is trusted
# instead of raw os.kill(pid, 0) liveness.
_tmp_log_dir = tempfile.mkdtemp(prefix="agent-host-test-")


def _exited_pid_with_log(payload):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    log_path = os.path.join(_tmp_log_dir, f"{proc.pid}-{time.time_ns()}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
    return proc.pid, log_path


pid_ok, log_ok = _exited_pid_with_log({"deliverable_id": "d", "ok": True, "grade": "pass"})
ok(agent_host.confirm_closure_verified({"pid": pid_ok, "log_path": log_ok}) is True,
   "an exited closure_verify job with no 'error' in its last JSON line counts as started")

pid_err, log_err = _exited_pid_with_log({"deliverable_id": "d", "error": "not found"})
ok(agent_host.confirm_closure_verified({"pid": pid_err, "log_path": log_err}) is False,
   "an exited closure_verify job whose own JSON reports 'error' is not treated as started")

pid_missing, _ = _exited_pid_with_log({"ignored": True})
ok(agent_host.confirm_closure_verified({"pid": pid_missing, "log_path": "/no/such/file"}) is False,
   "an exited closure_verify job with no readable log is not treated as started")

still_alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
try:
    ok(agent_host.confirm_closure_verified({"pid": still_alive.pid, "log_path": "/no/such/file"},
                                           grace_s=0.5) is True,
       "a closure_verify job still alive at the deadline counts as started (matches confirm_started)")
finally:
    still_alive.kill()
    still_alive.wait(timeout=5)

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
bound_wake = {
    "wake_id": "wake-byoa",
    "task_id": "CO-10",
    "selector": {
        "runtime": "claude-code", "agent_id": "claude/CO-10", "lane": "ADAPTER",
    },
    "policy": {"account_binding": {
        "tenant_id": "tenant-safe", "user_id": "user-safe", "project": "switchboard",
        "provider": "anthropic-claude", "provider_account_id": "account-safe",
        "credential_reference": "provider-cred-safe", "task_id": "CO-10",
        "account_affinity_id": "affinity-safe",
        "credential_admission_phase": "preclaim",
    }},
}


def fake_try_byoa(method, path, body=None):
    calls.append((method, path, body or {}))
    if path.startswith(agent_host.P_LIST_WAKES):
        return {"wake_intents": [bound_wake]}
    if path == agent_host.P_CLAIM_WAKE:
        claimed_wake = json.loads(json.dumps(bound_wake))
        claimed_wake["policy"]["account_binding"].update({
            "host_id": inventory["host_id"],
            "runner_session_id": body["runner_session_id"],
            "credential_admission_phase": "pending",
        })
        return {"claimed": True, "reserved": True,
                "credential_admission_phase": "pending", "wake": claimed_wake}
    if path == agent_host.P_COMPLETE_WAKE:
        return {"ok": True}
    return {"ok": True}


agent_host._try = fake_try_byoa
launch_envs = []
def fake_launch_byoa(wake, inv, runner_session_id="", extra_env=None):
    launch_envs.append(dict(extra_env or {}))
    return {
    "runner_session_id": runner_session_id,
    "pid": 12346,
    "wake_mode": agent_host.wake_mode(wake),
    }
agent_host.launch = fake_launch_byoa
summary = agent_host.run_once(inventory)
claim_index = next(i for i, call in enumerate(calls) if call[1] == agent_host.P_CLAIM_WAKE)
claim_body = calls[claim_index][2]
ok(claim_body["runner_session_id"].startswith("run_")
   and "credential_lease_id" not in claim_body
   and not any(call[1].endswith("/leases") for call in calls),
   "BYOA host reserves the wake before any credential lease exists")
ok(summary["acted"][0]["runner_session_id"] == claim_body["runner_session_id"],
   "the supervisor launches with the same runner identity that reserved the wake")
byoa_runner_registers = [
    call[2] for call in calls if call[1] == agent_host.P_REGISTER_RUNNER
]
ok(len(byoa_runner_registers) == 1
   and byoa_runner_registers[0]["metadata"]["credential_admission_phase"] == "preclaim"
   and launch_envs[0]["PM_REMOTE_WORK_SESSION_REGISTRATION"] == "1"
   and launch_envs[0]["PM_AUTO_WORK_SESSION"] == "1",
   "worker receives the reservation context and owns exact claim/session/lease rebinding")
ok(summary["acted"][0]["wake_completion_delegated"] is True
   and not any(call[1] == agent_host.P_COMPLETE_WAKE for call in calls),
   "Agent Host leaves BYOA wake completion to the credential-admitted child")

# A child that dies before provider admission cannot complete its own wake. The
# host must terminally record the preclaim runner and fail the wake so the Spot
# worker is not pinned forever by a claimed intent.
calls = []
agent_host._try = fake_try_byoa
agent_host.confirm_started = lambda rec: False
agent_host.launch = lambda wake, inv, runner_session_id="", extra_env=None: None
summary = agent_host.run_once(inventory)
agent_host.confirm_started = lambda rec: True
failed_runner_registers = [
    call[2] for call in calls
    if call[1] == agent_host.P_REGISTER_RUNNER
    and call[2].get("status") == "failed"
]
failed_wake_calls = [call[2] for call in calls if call[1] == agent_host.P_COMPLETE_WAKE]
ok(len(failed_runner_registers) == 1
   and failed_runner_registers[0]["runner_session_id"].startswith("run_")
   and failed_runner_registers[0]["metadata"]["credential_admission_phase"]
       == "preclaim_failed",
   "failed BYOA child replaces the starting runner row with terminal preclaim evidence")
ok(summary["acted"][0]["wake_completion_delegated"] is False
   and len(failed_wake_calls) == 1
   and failed_wake_calls[0]["result"]["started"] is False,
   "failed BYOA child is completed by the host instead of leaking a claimed wake")

# run_once end-to-end for a closure_verify wake: a fast, already-exited "job" must
# still be reported started=True (the bug this whole block guards against — before
# the confirm_closure_verified wiring, run_once used bare os.kill liveness for every
# mode and logged launch_failed for jobs that had already succeeded and exited).
calls = []
_closure_pid, _closure_log = _exited_pid_with_log(
    {"deliverable_id": "some-deliverable", "ok": True, "grade": "pass"})


def fake_try_closure(method, path, body=None):
    calls.append((method, path, body or {}))
    if path.startswith(agent_host.P_LIST_WAKES):
        return {"wake_intents": [closure_wake]}
    if path == agent_host.P_CLAIM_WAKE:
        return {"claimed": True}
    if path == agent_host.P_COMPLETE_WAKE:
        return {"ok": True}
    return {"ok": True}


agent_host._try = fake_try_closure
agent_host.launch = lambda wake, inv: {
    "runner_session_id": "run_closure_test", "pid": _closure_pid, "log_path": _closure_log,
    "wake_mode": agent_host.wake_mode(wake),
}
summary = agent_host.run_once(message_only_inventory)
complete_calls = [c for c in calls if c[1] == agent_host.P_COMPLETE_WAKE]
ok(summary["acted"] and summary["acted"][0]["wake_mode"] == "closure_verify",
   "run_once reports closure_verify mode for a closure wake")
ok(summary["acted"] and summary["acted"][0]["started"] is True,
   "run_once reports started=True for a fast-but-successful closure_verify job (the fix)")
ok(complete_calls and complete_calls[0][2]["result"]["reason"] == "started",
   "complete_wake records 'started', not a misleading 'launch_failed', for that job")

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
