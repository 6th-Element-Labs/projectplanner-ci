#!/usr/bin/env python3
"""UI-29: Autopilot is Running only after an exact claim/session bind."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile


TMP = Path(tempfile.mkdtemp(prefix="ui29-"))
os.environ.update({
    "PM_SWITCHBOARD_DB_PATH": str(TMP / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(TMP / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(TMP / "projects"),
    "PM_PROJECT": "switchboard",
})
(TMP / "projects").mkdir()

import store  # noqa: E402
import mission_coordinator  # noqa: E402
from adapters import agent_host, codex_local_worker, switchboard_core  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


# Runtime selection is exact. A global Claude module may not execute a Codex wake;
# the runtime-specific Codex mapping wins when it is configured.
inventory = {
    "host_id": "host/ui29",
    "repo_root": str(TMP),
    "policy": {"allow_work": True, "allow_global_claim": False},
    "runtimes": [{"runtime": "codex", "lanes": ["UI"], "capabilities": []}],
}
wake = {
    "wake_id": "wake-ui29-runtime", "task_id": "UI-29",
    "selector": {"runtime": "codex", "agent_id": "codex/UI-29", "lane": "UI"},
    "policy": {"mode": "claim_next"},
}
os.environ["PM_AGENT_WORK_MODULE"] = "adapters.claude_personal_worker:run"
os.environ.pop("PM_AGENT_WORK_MODULE_CODEX", None)
try:
    agent_host.launch_command(wake, inventory)
    mismatch_denied = False
except ValueError:
    mismatch_denied = True
ok(mismatch_denied, "a Claude work module cannot execute a Codex wake")
os.environ["PM_AGENT_WORK_MODULE_CODEX"] = "adapters.codex_local_worker:run"
command, _ = agent_host.launch_command(wake, inventory)
ok("adapters.codex_local_worker:run" in command,
   "runtime-specific Codex worker mapping overrides the global module")


# A generic Agent Host creates one task/runner-isolated worktree and cleans only
# that owned workspace after completion.
source = TMP / "source"
source_origin = TMP / "source-origin.git"
workspace_root = TMP / "workspaces"
subprocess.run(["git", "init", "--bare", str(source_origin)], check=True,
               stdout=subprocess.DEVNULL)
source.mkdir()
subprocess.run(["git", "init", "-b", "master", str(source)], check=True,
               stdout=subprocess.DEVNULL)
subprocess.run(["git", "-C", str(source), "config", "user.email", "ui29@test"], check=True)
subprocess.run(["git", "-C", str(source), "config", "user.name", "UI29"], check=True)
(source / "README").write_text("ui29\n", encoding="utf-8")
subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
subprocess.run(["git", "-C", str(source), "commit", "-m", "base"], check=True,
               stdout=subprocess.DEVNULL)
subprocess.run(["git", "-C", str(source), "remote", "add", "origin",
                str(source_origin)], check=True)
original_http = switchboard_core._http
switchboard_core._http = lambda *_args, **_kwargs: {
    "work_session": {"work_session_id": "worksession-ui29-local"}}
os.environ.update({
    "PM_AGENT_HOST_ISOLATE_TASK_WORKSPACE": "1",
    "PM_WORKSPACE_ROOT": str(workspace_root),
    "PM_RUNNER_SESSION_ID": "run-ui29-local",
})
managed = switchboard_core.create_external_work_session(
    "switchboard", "UI-29", "codex/UI-29", "codex", str(source))
switchboard_core._http = original_http
ok(managed["workspace_path"] != str(source)
   and Path(managed["workspace_path"]).is_dir()
   and managed["worker_owned_workspace"] is True,
   "each generic wake gets a runner-isolated task worktree")
cleaned = switchboard_core.cleanup_external_work_session(managed)
ok(cleaned.get("cleaned") is True and not Path(managed["workspace_path"]).exists(),
   "Agent Host removes only its owned task worktree")


# The host-side binding barrier accepts only the exact runner/claim/session tuple.
bound_row = {
    "runner_session_id": "run-ui29-bind", "task_id": "UI-29",
    "host_id": "host/ui29", "agent_id": "codex/UI-29", "runtime": "codex",
    "claim_id": "taskclaim-ui29",
    "status": "running", "stale": False,
    "metadata": {"wake_id": "wake-ui29-bind",
                 "work_session_id": "worksession-ui29",
                 "credential_admission_phase": "claim_bound"},
}
polls = iter([
    {"sessions": [{**bound_row, "claim_id": "", "metadata": {
        **bound_row["metadata"], "credential_admission_phase": "preclaim",
    }}]},
    {"sessions": [bound_row]},
])
original_try = agent_host._try
agent_host._try = lambda *_args, **_kwargs: next(polls)
clock = [0.0]
def fake_sleep(seconds):
    clock[0] += seconds
barrier = agent_host.wait_for_runner_binding(
    {"wake_id": "wake-ui29-bind", "task_id": "UI-29",
     "selector": {"agent_id": "codex/UI-29", "runtime": "codex"}},
    {"host_id": "host/ui29"}, "run-ui29-bind", timeout_s=5,
    sleep=fake_sleep, monotonic=lambda: clock[0])
agent_host._try = original_try
ok(barrier.get("bound") is True
   and (barrier.get("session") or {}).get("claim_id") == "taskclaim-ui29",
   "Running waits for the exact claim-bound claim and Work Session bind")


# Sessions launched earlier in one poll are already present in the supervisor's
# live count. They must not be counted again via the acted receipt list, or an
# eight-session host stops after four launches.
fanout_wakes = [{
    "wake_id": f"wake-ui29-fanout-{index}",
    "selector": {"runtime": "codex", "agent_id": f"codex/fanout-{index}",
                 "lane": "UI"},
    "policy": {"mode": "claim_next"},
} for index in range(8)]
fanout_inventory = {
    "host_id": "host/ui29-fanout", "repo_root": str(source),
    "limits": {"max_sessions": 8},
    "policy": {"allow_work": True, "allow_global_claim": False},
    "runtimes": [{"runtime": "codex", "lanes": ["UI"], "capabilities": []}],
}
launches = []
original_launch = agent_host.launch
original_confirm_started = agent_host.confirm_started
original_active_session_count = agent_host.active_session_count
def fake_fanout_try(method, path, body=None):
    if path.startswith(agent_host.P_LIST_WAKES):
        return {"wake_intents": fanout_wakes}
    if path == agent_host.P_CLAIM_WAKE:
        return {"claimed": True}
    return {"ok": True}
agent_host._try = fake_fanout_try
agent_host.active_session_count = lambda _inventory: len(launches)
def fake_fanout_launch(wake, _inventory):
    launches.append(wake["wake_id"])
    return {"runner_session_id": f"run-{len(launches)}", "pid": 1000 + len(launches),
            "wake_mode": "claim_next"}
agent_host.launch = fake_fanout_launch
agent_host.confirm_started = lambda _record: True
fanout = agent_host.run_once(fanout_inventory)
agent_host._try = original_try
agent_host.launch = original_launch
agent_host.confirm_started = original_confirm_started
agent_host.active_session_count = original_active_session_count
ok(len(launches) == 8 and len(fanout.get("acted") or []) == 8,
   "an eight-session host launches all eight available slots in one poll")


# The durable complete_wake boundary independently rejects process-only startup.
store.init_db("switchboard")
task = store.create_task({"workstream_id": "UI", "title": "UI-29 bind gate"},
                         actor="ui29-test", project="switchboard")
task_id = task["task_id"]
host_id = "host/ui29-store"
runner_id = "run-ui29-store"
agent_id = f"codex/{task_id}"
store.register_agent(agent_id, "codex", lane="UI", actor="ui29-test",
                     project="switchboard")
store.register_host({
    "host_id": host_id,
    "runtimes": [{"runtime": "codex", "lanes": ["UI"]}],
    "capacity": {"max_sessions": 8, "active_sessions": 0},
    "heartbeat_ttl_s": 3600,
}, actor=host_id, project="switchboard")
requested = store.request_wake(
    {"runtime": "codex", "lane": "UI", "agent_id": agent_id},
    task_id=task_id, actor="ui29-test", project="switchboard",
    policy={"mode": "claim_next", "require_runner_bind": True})
store.claim_wake(host_id, requested["wake_id"], runner_session_id=runner_id,
                 actor=host_id, project="switchboard")
store.upsert_runner_session({
    "runner_session_id": runner_id, "host_id": host_id,
    "agent_id": agent_id, "runtime": "codex", "task_id": task_id,
    "status": "starting", "metadata": {
        "credential_admission_phase": "preclaim", "wake_id": requested["wake_id"]},
}, actor=host_id, project="switchboard")
rejected = store.complete_wake(
    requested["wake_id"], runner_session_id=runner_id, agent_id=agent_id,
    result={"started": True}, actor=host_id, project="switchboard")
ok(rejected.get("error_code") == "runner_bind_incomplete"
   and rejected.get("retryable") is True,
   "process-only startup cannot terminally complete an Autopilot wake")

head = subprocess.check_output(
    ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
work_session_id = "worksession-ui29-store"
store.create_work_session({
    "work_session_id": work_session_id, "task_id": task_id,
    "agent_id": agent_id, "runtime": "codex", "repo_role": "canonical",
    "branch": f"codex/{task_id}-ui29", "upstream": "origin/master",
    "base_sha": head, "head_sha": head, "storage_mode": "worktree",
    "worktree_path": str(source), "status": "active", "dirty_status": "clean",
    "conflict_marker_count": 0, "policy_profile": "code_strict",
}, actor="ui29-test", project="switchboard")
claimed = store.claim_task(
    task_id, agent_id, work_session_id=work_session_id,
    require_work_session=True, session_policy_profile="code_strict",
    actor="ui29-test", project="switchboard")
store.upsert_runner_session({
    "runner_session_id": runner_id, "host_id": host_id,
    "agent_id": agent_id, "runtime": "claude-code", "task_id": task_id,
    "claim_id": claimed["claim_id"], "status": "running",
    "metadata": {"credential_admission_phase": "claim_bound",
                 "wake_id": requested["wake_id"],
                 "work_session_id": work_session_id},
    "require_task_bind": True, "heartbeat_ttl_s": 3600,
}, actor=agent_id, project="switchboard")
wrong_runtime = store.complete_wake(
    requested["wake_id"], runner_session_id=runner_id, agent_id=agent_id,
    result={"started": True, "claim_id": claimed["claim_id"]},
    actor=host_id, project="switchboard")
ok(wrong_runtime.get("error_code") == "runner_bind_incomplete",
   "a claim-bound runner with the wrong requested runtime cannot report Running")
store.upsert_runner_session({
    "runner_session_id": runner_id, "host_id": host_id,
    "agent_id": agent_id, "runtime": "codex", "task_id": task_id,
    "claim_id": claimed["claim_id"], "status": "running",
    "metadata": {"credential_admission_phase": "claim_bound",
                 "wake_id": requested["wake_id"],
                 "work_session_id": work_session_id},
    "require_task_bind": True, "heartbeat_ttl_s": 3600,
}, actor=agent_id, project="switchboard")
accepted = store.complete_wake(
    requested["wake_id"], runner_session_id=runner_id, agent_id=agent_id,
    result={"started": True, "claim_id": claimed["claim_id"]},
    actor=host_id, project="switchboard")
ok(accepted.get("status") == "completed",
   "wake completes after the exact active claim and Work Session are durable")


# Coordinator retry generations suppress active duplicates and advance after a
# failed/legacy terminal attempt.
class WakeStore:
    def __init__(self):
        self.rows = [{
            "wake_id": "wake-old", "task_id": "UI-99", "status": "failed",
            "requested_at": 1,
            "selector": {"deliverable_id": "deliverable-ui29"},
        }]
        self.calls = []
    def list_wake_intents(self, **_kwargs):
        return list(self.rows)
    def request_wake(self, selector, **kwargs):
        self.calls.append({"selector": selector, **kwargs})
        return {"wake_id": "wake-retry", "status": "pending"}
    def record_coordinator_decision(self, **kwargs):
        return {"decision_id": "decision-ui29", **kwargs}
    def append_activity(self, *_args, **_kwargs):
        return 1

fake = WakeStore()
mission = {
    "deliverable_id": "deliverable-ui29",
    "progress": {"linked_task_count": 1, "done_with_proof_ratio": 0},
    "dispatch_scope": {"blocking_task_count": 1,
                       "blocking_done_with_proof_ratio": 0},
    "next_actions": [{"action": "claim_task", "task_id": "UI-99",
                      "project_id": "switchboard", "lane": "UI"}],
}
mission_coordinator.run_coordinator_tick(
    mission, mission_project="switchboard", store_mod=fake,
    policy={"auto_refresh_brief": False, "auto_claim": False, "auto_wake": True,
            "worker_wake_selector": {"runtime": "codex"},
            "worker_wake_policy": {"mode": "co_fleet"}},
    actor="ui29-test")
call = fake.calls[0]
ok(call["idem_key"].endswith("-2")
   and call["policy"]["dispatch_attempt"] == 2
   and call["policy"]["require_runner_bind"] is True,
   "failed pre-claim startup advances one crash-safe retry generation")
fake.rows.append({
    "wake_id": "wake-active", "task_id": "UI-99", "status": "pending",
    "requested_at": 2, "selector": {"deliverable_id": "deliverable-ui29"},
})
mission_coordinator.run_coordinator_tick(
    mission, mission_project="switchboard", store_mod=fake,
    policy={"auto_refresh_brief": False, "auto_claim": False, "auto_wake": True,
            "worker_wake_selector": {"runtime": "codex"},
            "worker_wake_policy": {"mode": "co_fleet"}},
    actor="ui29-test")
ok(len(fake.calls) == 1, "an active exact wake suppresses duplicate dispatch")


# Native Codex host-local mode accepts the claim and managed Work Session created
# by run_session without requiring a personal-account execution binding.
bare = TMP / "bare.git"
worker = TMP / "worker"
subprocess.run(["git", "init", "--bare", str(bare)], check=True,
               stdout=subprocess.DEVNULL)
subprocess.run(["git", "clone", str(bare), str(worker)], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(["git", "-C", str(worker), "config", "user.email", "ui29@test"], check=True)
subprocess.run(["git", "-C", str(worker), "config", "user.name", "UI29"], check=True)
(worker / "TASK").write_text("ready\n", encoding="utf-8")
subprocess.run(["git", "-C", str(worker), "add", "TASK"], check=True)
subprocess.run(["git", "-C", str(worker), "commit", "-m", "worker"], check=True,
               stdout=subprocess.DEVNULL)
subprocess.run(["git", "-C", str(worker), "push", "-u", "origin", "master"], check=True,
               stdout=subprocess.DEVNULL)
worker_head = subprocess.check_output(
    ["git", "-C", str(worker), "rev-parse", "HEAD"], text=True).strip()
os.environ.update({
    "PM_CO_ACCOUNT_BINDING_JSON": "{}",
    "PM_PERSONAL_AGENT_HOST_EXECUTION": "0",
    "PM_HOST_ID": "host/ui29-native",
    "PM_RUNNER_SESSION_ID": "run-ui29-native",
    "PM_CO_WAKE_ID": "wake-ui29-native",
    "PM_AGENT_ID": "codex/UI-29-native",
})
updates = []
def fake_http(method, path, body=None, **_kwargs):
    updates.append((method, path, dict(body or {})))
    return {"ok": True, "runner_session_id": "run-ui29-native"}
evidence = codex_local_worker.run({
    "task_id": "UI-29", "claim_id": "taskclaim-ui29-native",
    "task": {"task_id": "UI-29", "title": "Native", "description": "No-op"},
    "managed": {"work_session_id": "worksession-ui29-native",
                "workspace_path": str(worker), "head_sha": worker_head},
}, codex_executable="/usr/bin/true", http=fake_http,
   runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""))
lifecycle = evidence.pop("_switchboard_personal_execution_lifecycle")
lifecycle["complete"](evidence)
running = next(body for _method, path, body in updates
               if path == "/ixp/v1/register_runner_session"
               and body.get("status") == "running")
ok(running["claim_id"] == "taskclaim-ui29-native"
   and running["metadata"]["work_session_id"] == "worksession-ui29-native"
   and running["metadata"]["auth_lane"] == "codex_host_local",
   "native Codex publishes the generic exact claim/session bind and heartbeat lifecycle")

print(f"\nUI-29 execution closure: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
