#!/usr/bin/env python3
"""BUG-86: selected Mac starts one native CLI before any scheduler handshake."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401
from adapters import agent_host
from adapters import direct_codex_session


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


host_id = "host/direct-mac"
task_id = "BUG-86"
assignment = {
    "schema": "switchboard.direct_cli_assignment.v1",
    "project": "switchboard",
    "task_id": task_id,
    "deliverable_id": "agent-host-autopilot",
    "host_id": host_id,
    "agent_id": f"codex/{task_id}",
    "prompt": "Do BUG-86 for deliverable agent-host-autopilot in project switchboard via Switchboard.",
    "repository": {
        "slug": "6th-Element-Labs/projectplanner",
        "default_branch": "master",
    },
    "mcp": {"endpoint": "https://plan.taikunai.com/mcp"},
}
wake = {
    "wake_id": "wake-direct-bug86",
    "task_id": task_id,
    "selector": {
        "runtime": "codex", "lane": "BUG", "agent_id": f"codex/{task_id}",
        "task_id": task_id, "host_id": host_id,
    },
    "policy": {
        "mode": "direct_task", "execution_mode": "direct_personal_cli",
        "require_runner_bind": False, "assignment": assignment,
    },
}
inventory = {
    "host_id": host_id,
    "repo_root": str(ROOT),
    "policy": {
        "allow_work": True, "allow_global_claim": False,
        "lane_mode": "all_project_lanes",
    },
    "limits": {"max_sessions": 8},
    "runtimes": [{
        "runtime": "codex", "lanes": [], "capabilities": [],
        "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
    }],
}

command, mode = agent_host.launch_command(
    wake, inventory, runner_session_id="run-direct-bug86")
ok(mode == "direct_task" and str(agent_host.DIRECT_CODEX_SESSION) in command,
   "direct assignment launches the dedicated native Codex session helper")
ok("run_agent.py" not in " ".join(command),
   "direct assignment cannot fall into the claim-next worker loop")
ok(command[command.index("--wake-id") + 1] == wake["wake_id"]
   and command[command.index("--wake-mode") + 1] == "direct_task",
   "the supervisor persists the wake identity needed for later runner heartbeats")

events = []


def fake_try(method, path, body=None):
    events.append(("http", method, path, body or {}))
    if path.startswith(agent_host.P_LIST_WAKES):
        return {"wake_intents": [wake]}
    if path == agent_host.P_REGISTER_RUNNER:
        return {"runner_session_id": (body or {}).get("runner_session_id")}
    if path == agent_host.P_COMPLETE_WAKE:
        return {"completed": True}
    return {"ok": True}


def fake_launch(selected, inv, runner_session_id="", extra_env=None):
    events.append(("launch", runner_session_id, dict(extra_env or {})))
    return {
        "runner_session_id": runner_session_id,
        "agent_id": f"codex/{task_id}",
        "runtime": "codex",
        "task_id": task_id,
        "pid": 43210,
        "status": "running",
        "cwd": str(ROOT),
        "control": {
            "tier": "T3", "runner_kill": True, "managed_process": True,
            "runner_open": True, "runner_inject": True, "runner_logs": True,
        },
        "pty": True,
        "wake_mode": "direct_task",
    }


agent_host._try = fake_try
agent_host.launch = fake_launch
agent_host.confirm_started = lambda rec: True
agent_host.active_session_count = lambda inv: 0
agent_host.supervisor_action = lambda action, runner_session_id, options=None: {
    "error": "not_found"}
agent_host._reap_bound_finalizers = lambda selected_host: []
agent_host.heartbeat_capacity = lambda inv: {
    "active_sessions": 0,
    "local_auth": {"available": True},
}
agent_host.apply_authoritative_execution_policy = lambda inv, heartbeat: False
agent_host.handle_runner_controls = lambda inv: []

summary = agent_host.run_once(inventory)
paths = [event[2] for event in events if event[0] == "http"]
launch_index = next(i for i, event in enumerate(events) if event[0] == "launch")
register_index = next(
    i for i, event in enumerate(events)
    if event[0] == "http" and event[2] == agent_host.P_REGISTER_RUNNER)
complete_index = next(
    i for i, event in enumerate(events)
    if event[0] == "http" and event[2] == agent_host.P_COMPLETE_WAKE)

ok(agent_host.P_CLAIM_WAKE not in paths,
   "Mac boot does not call the scheduler ownership endpoint")
ok(launch_index < register_index < complete_index,
   "PTY starts first, then becomes watchable, then the assignment is acknowledged")
ok(events[register_index][3]["heartbeat_ttl_s"] == 180,
   "direct PTY registration survives a busy daemon tick without flickering stale")
launch_event = events[launch_index]
loaded = json.loads(launch_event[2]["PM_DIRECT_CODEX_ASSIGNMENT_JSON"])
ok(loaded == assignment,
   "the daemon passes the exact task/repo/prompt/MCP assignment to the CLI boot")
with tempfile.TemporaryDirectory(prefix="bug86-assignment-") as assignment_tmp:
    runner_root = Path(assignment_tmp) / "runners"
    (runner_root / "run-direct-bug86").mkdir(parents=True)
    old_runner_dir = os.environ.get("PM_AGENT_HOST_RUNNER_DIR")
    old_runner_id = os.environ.get("PM_RUNNER_SESSION_ID")
    try:
        os.environ["PM_AGENT_HOST_RUNNER_DIR"] = str(runner_root)
        os.environ["PM_RUNNER_SESSION_ID"] = "run-direct-bug86"
        assignment_path = direct_codex_session._write_assignment_toml(
            assignment, Path(assignment_tmp) / "workspace",
            "codex/bug-86-direct", "a" * 40)
        assignment_text = assignment_path.read_text(encoding="utf-8")
    finally:
        if old_runner_dir is None:
            os.environ.pop("PM_AGENT_HOST_RUNNER_DIR", None)
        else:
            os.environ["PM_AGENT_HOST_RUNNER_DIR"] = old_runner_dir
        if old_runner_id is None:
            os.environ.pop("PM_RUNNER_SESSION_ID", None)
        else:
            os.environ["PM_RUNNER_SESSION_ID"] = old_runner_id
ok('agent_id = "codex/BUG-86"' in assignment_text,
   "assignment TOML pins the exact MCP-bound agent identity")
ok(summary["acted"][0]["started"] is True
   and summary["acted"][0]["runner_registered"] is True
   and summary["acted"][0]["completion_recorded"] is True,
   "one direct daemon tick reports a live browser-visible Codex session")

# Production hosts retain historical runner evidence.  Renewal must never pull
# every stale row for the host before it can heartbeat the few local PTYs that
# are actually alive.
bounded_calls = []
saved_try = agent_host._try
saved_subprocess_run = agent_host.subprocess.run
agent_host.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
    returncode=0,
    stdout=json.dumps({"sessions": [{
        "runner_session_id": "run-direct-bug86", "task_id": task_id,
        "status": "running", "alive": True,
    }, {
        "runner_session_id": "run-old-dead", "task_id": "OLD-1",
        "status": "failed", "alive": False,
    }]}),
)


def bounded_try(method, path, body=None):
    bounded_calls.append((method, path))
    return {"sessions": [{
        "runner_session_id": "run-direct-bug86", "task_id": task_id,
        "status": "running", "metadata": {
            "wake_id": wake["wake_id"], "direct_assignment": True,
        },
    }]}


agent_host._try = bounded_try
bounded_rows = agent_host._drain_runners(host_id)
agent_host._try = saved_try
agent_host.subprocess.run = saved_subprocess_run
ok(len(bounded_calls) == 1
   and f"task_id={task_id}" in bounded_calls[0][1]
   and "include_stale=true" in bounded_calls[0][1]
   and all("task_id=OLD-1" not in path for _, path in bounded_calls)
   and bounded_rows[0]["alive"] is True
   and bounded_rows[0]["metadata"]["wake_id"] == wake["wake_id"],
   "runner renewal fetches stale history only for locally-live task ids")

# The wake leaves the pending feed after launch, but the native CLI can run for
# hours.  Every later daemon tick must renew the exact local PTY row so closing
# and reopening Watch still resolves it after the original registry TTL.
heartbeat_calls = []
agent_host._drain_runners = lambda selected_host: [{
    "runner_session_id": "run-direct-bug86",
    "agent_id": f"codex/{task_id}",
    "runtime": "codex",
    "task_id": task_id,
    "host_id": selected_host,
    "pid": 43210,
    "status": "running",
    "alive": True,
    "cwd": str(ROOT),
    "control": {"tier": "T3", "runner_open": True, "runner_inject": True},
    "metadata": {
        "wake_id": wake["wake_id"],
        "direct_assignment": True,
        "native_host_execution": True,
        "assignment_schema": "switchboard.direct_cli_assignment.v1",
    },
}]


def fake_heartbeat(method, path, body=None):
    heartbeat_calls.append((method, path, dict(body or {})))
    return {"runner_session_id": (body or {}).get("runner_session_id"), "status": "running"}


agent_host._try = fake_heartbeat
renewed = agent_host.renew_live_direct_runners(inventory)
renewal = next(row for row in heartbeat_calls
               if row[0:2] == ("POST", agent_host.P_HEARTBEAT_RUNNER))
ok(renewed == [{
       "runner_session_id": "run-direct-bug86", "task_id": task_id,
       "renewed": True, "error": None,
   }]
   and renewal[0:2] == ("POST", agent_host.P_HEARTBEAT_RUNNER)
   and renewal[2]["metadata"]["wake_id"] == wake["wake_id"]
   and renewal[2]["task_id"] == task_id
   and renewal[2]["claim_id"] == ""
   and renewal[2]["heartbeat_ttl_s"] == 180,
   "each daemon tick renews the live direct task PTY without inventing claim state")

# Autopilot workers heartbeat claim state from inside the child, but only the
# supervisor knows the PTY coordinates. The host must continuously join both.
heartbeat_calls.clear()
agent_host._drain_runners = lambda selected_host: [{
    "runner_session_id": "run-autopilot-watch",
    "agent_id": "codex/ARCH-MS-123", "runtime": "codex",
    "task_id": "ARCH-MS-123", "host_id": selected_host,
    "claim_id": "taskclaim-123", "pid": 54321,
    "status": "running", "alive": True, "cwd": str(ROOT),
    "wake_mode": "claim_next",
    "control": {"tier": "T3", "runner_open": True, "runner_inject": True},
    "metadata": {
        "wake_id": "wake-123", "work_session_id": "worksession-123",
        "credential_admission_phase": "claim_bound", "pty": True,
        "stream_bind": "127.0.0.1", "stream_port": 64123,
        "native_host_execution": True,
    },
}]
renewed_bound = agent_host.renew_live_direct_runners(inventory)
bound_renewal = heartbeat_calls[0][2]
ok(renewed_bound[0]["renewed"] is True
   and bound_renewal["claim_id"] == "taskclaim-123"
   and bound_renewal["metadata"]["work_session_id"] == "worksession-123"
   and bound_renewal["metadata"]["pty"] is True
   and bound_renewal["metadata"]["stream_port"] == 64123
   and bound_renewal["metadata"].get("direct_assignment") is not True,
   "each daemon tick repairs and renews claim-bound Autopilot PTY transport")

# A failed session can leave its branch attached to the first worktree.  A
# deliberate retry must therefore create a second worktree with a different
# branch instead of dying before Codex starts.
with tempfile.TemporaryDirectory(prefix="bug86-retry-") as tmp_raw:
    tmp = Path(tmp_raw)
    remote = tmp / "remote.git"
    source = tmp / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    subprocess.run(["git", "init", "-b", "master", str(source)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email",
                    "bug86@example.local"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name",
                    "BUG-86 test"], check=True)
    (source / "README.md").write_text("retry proof\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin",
                    str(remote)], check=True)
    subprocess.run(["git", "-C", str(source), "push", "-u", "origin", "master"],
                   check=True, capture_output=True)

    retry_assignment = {
        **assignment,
        "repository": {**assignment["repository"], "branch": "codex/bug-86"},
    }
    old_env = {key: os.environ.get(key) for key in (
        "PM_AGENT_HOST_SOURCE_REPO_ROOT", "PM_PERSONAL_WORKSPACE_ROOT",
        "PM_RUNNER_SESSION_ID",
    )}
    try:
        os.environ["PM_AGENT_HOST_SOURCE_REPO_ROOT"] = str(source)
        os.environ["PM_PERSONAL_WORKSPACE_ROOT"] = str(tmp / "workspaces")
        os.environ["PM_RUNNER_SESSION_ID"] = "run-first"
        first_workspace, first_branch, _ = direct_codex_session._prepare_workspace(
            retry_assignment)
        os.environ["PM_RUNNER_SESSION_ID"] = "run-retry"
        retry_workspace, retry_branch, _ = direct_codex_session._prepare_workspace(
            retry_assignment)
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    ok(first_workspace != retry_workspace and first_branch != retry_branch
       and first_branch.startswith("codex/bug-86-direct-")
       and retry_branch.startswith("codex/bug-86-direct-"),
       "a retry gets a fresh deterministic task branch and worktree")

print(f"\nBUG-86 direct native CLI: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
