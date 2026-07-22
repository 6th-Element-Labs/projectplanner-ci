#!/usr/bin/env python3
"""BUG-103: reload launchd config and late-bind direct task Work Sessions."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from path_setup import ROOT  # noqa: F401
from adapters import agent_host
from adapters import agent_host_enrollment as enrollment
from adapters.codex import supervisor


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


class Result:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


calls = []


def service_runner(command, **_kwargs):
    calls.append(list(command))
    return Result(3 if command[1] == "bootout" else 0)


enrollment.control_service(
    "darwin", "restart", Path("/tmp/com.6thelement.switchboard-agent-host.plist"),
    runner=service_runner,
)
ok([row[1] for row in calls] == ["bootout", "bootstrap"],
   "macOS restart unloads and reloads the plist instead of kickstarting stale config")
ok(calls[1][-1].endswith("com.6thelement.switchboard-agent-host.plist"),
   "bootstrap reloads the exact rendered service definition")

saved_path = supervisor.os.environ.get("PATH")
try:
    supervisor.os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    child_env = supervisor._runner_environment(platform_name="darwin")
finally:
    if saved_path is None:
        supervisor.os.environ.pop("PATH", None)
    else:
        supervisor.os.environ["PATH"] = saved_path
ok(child_env["PATH"].split(":")[:2] == ["/opt/homebrew/bin", "/usr/local/bin"],
   "runner launch normalizes Homebrew PATH even when its parent daemon is stale")
if sys.platform == "darwin":
    ok(shutil.which("gh", path=child_env["PATH"]) == "/opt/homebrew/bin/gh",
       "the exact child environment resolves the installed gh binary")
else:
    ok(child_env["PATH"].split(":")[0] == "/opt/homebrew/bin",
       "non-Mac CI verifies the Homebrew gh search path structurally")


runner = {
    "runner_session_id": "run_bug103",
    "task_id": "BUG-103",
    "agent_id": "codex/BUG-103",
    "claim_id": None,
    "metadata": {
        "direct_assignment": True,
        "native_host_execution": True,
        "credential_admission_phase": "preclaim",
        "wake_id": "wake-bug103",
        "assignment_schema": "switchboard.direct_cli_assignment.v1",
    },
}
work_session = {
    "work_session_id": "worksession-bug103",
    "claim_id": "taskclaim-bug103",
    "task_id": "BUG-103",
    "agent_id": "codex/BUG-103",
    "principal_id": "direct-session/run_bug103",
    "status": "active",
}
binding = agent_host._direct_work_session_binding(runner, [work_session])
ok(binding and binding["work_session_id"] == "worksession-bug103",
   "direct runner joins the Work Session created by its exact direct-session principal")
ok(agent_host._direct_work_session_binding(
    runner, [work_session, {**work_session, "work_session_id": "ambiguous"}]) is None,
   "ambiguous Work Sessions fail closed instead of guessing")
ok(agent_host._direct_work_session_binding(
    runner, [{**work_session, "principal_id": "direct-session/run_other"}]) is None,
   "another runner principal cannot bind this execution")
completed_binding = agent_host._direct_work_session_binding(
    runner, [{**work_session, "status": "completed"}],
    allowed_statuses={"completed"},
)
ok(completed_binding and completed_binding["work_session_id"] == "worksession-bug103",
   "a fast completed Work Session can still close the runner binding race")


posted = []
saved = {
    "try": agent_host._try,
    "runners": agent_host._drain_runners,
    "sessions": agent_host._drain_work_sessions,
    "preflight": agent_host._host_repo_preflight,
}
try:
    agent_host._drain_runners = lambda _host: [{
        **runner,
        "host_id": "host/bug103-mac",
        "runtime": "codex",
        "status": "running",
        "alive": True,
        "pid": 123,
        "cwd": "/source/repo",
        "control": {"runner_open": True},
    }]
    agent_host._drain_work_sessions = lambda **_filters: [work_session]
    agent_host._host_repo_preflight = lambda _rec, _inv, metadata=None: {
        "schema": "switchboard.repo_preflight.v1",
        "work_session_id": (metadata or {}).get("work_session_id"),
        "ok": True,
        "verdict": "pass",
    }

    def fake_try(method, path, body=None):
        if method == "POST":
            posted.append(dict(body or {}))
        return {"ok": True}

    agent_host._try = fake_try
    agent_host.renew_live_direct_runners({
        "host_id": "host/bug103-mac", "repo_root": "/source/repo",
    })
finally:
    agent_host._try = saved["try"]
    agent_host._drain_runners = saved["runners"]
    agent_host._drain_work_sessions = saved["sessions"]
    agent_host._host_repo_preflight = saved["preflight"]

heartbeat = posted[-1] if posted else {}
ok(heartbeat.get("claim_id") == "taskclaim-bug103",
   "normal host heartbeat publishes the late claim binding")
ok((heartbeat.get("metadata") or {}).get("work_session_id")
   == "worksession-bug103",
   "normal host heartbeat publishes the late Work Session binding")
ok((heartbeat.get("metadata") or {}).get("host_repo_preflight", {}).get("ok") is True,
   "the first claim-bound heartbeat carries host preflight evidence")


with tempfile.TemporaryDirectory(prefix="bug103-supervisor-") as raw:
    root = Path(raw)
    workspace = root / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "bug103@example.com"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "BUG-103"], check=True)
    (workspace / "proof.txt").write_text("proof\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "proof.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "proof"], check=True)
    session_root = root / "run_bug103"
    session_root.mkdir()
    (session_root / "assignment.toml").write_text(
        "[repository]\nworkspace = " + json.dumps(str(workspace)) + "\n",
        encoding="utf-8",
    )
    snap = supervisor._snapshot({
        "runner_session_id": "run_bug103",
        "task_id": "BUG-103",
        "cwd": "/definitely/not/the/workspace",
    }, root)
    ok(snap.get("cwd") == str(workspace.resolve()),
       "supervisor snapshots the task workspace from the durable assignment")
    ok(len(str(snap.get("head_sha") or "")) == 40,
       "task-workspace snapshot carries an exact Git head")


print(f"\nBUG-103 Agent Host path/binding: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
