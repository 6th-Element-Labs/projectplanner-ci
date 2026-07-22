#!/usr/bin/env python3
"""BUG-139: Connect injects a minted session bearer, never the host token."""
from __future__ import annotations

import os

from path_setup import ROOT  # noqa: F401

from adapters import agent_host


def _wake():
    return {
        "wake_id": "wake-bug139",
        "task_id": "BUG-139",
        "selector": {
            "runtime": "codex", "task_id": "BUG-139",
            "agent_id": "agent/codex/bug-139",
        },
        "policy": {"mode": "connect", "assignment": {
            "schema": "switchboard.connect.assignment.v1",
            "assignment_id": "assignment-bug139",
            "principal_ref": "agent/codex/bug-139",
            "work_ref": "task:switchboard:BUG-139",
            "runtime": "codex", "provider": "openai",
            "workspace_ref": "repo:canonical", "queued_at": 1.0,
            "limits": {"max_runtime_seconds": 7200,
                       "spend_limit_microunits": 0, "memory_limit_bytes": 0},
        }},
    }


inventory = {
    "host_id": "host/bug139", "repo_root": str(ROOT),
    "policy": {"allow_global_claim": False, "allow_work": True,
               "lane_mode": "all_project_lanes"},
    "runtimes": [{"runtime": "codex", "provider": "openai", "lanes": [],
                  "policy": {"allow_work": True,
                             "lane_mode": "all_project_lanes"}}],
}

saved_http = agent_host.sb._http
saved_run = agent_host.subprocess.run
saved_host_token = os.environ.get("PM_MCP_TOKEN")
captured = {}
try:
    os.environ["PM_MCP_TOKEN"] = "narrow-host-token"

    def fake_http(method, path, body=None):
        captured["request"] = (method, path, dict(body or {}))
        return {"issued": True, "token": "dst-task-session"}

    class Receipt:
        returncode = 0
        stdout = '{"runner_session_id":"run_bug139","status":"running"}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return Receipt()

    agent_host.sb._http = fake_http
    agent_host.subprocess.run = fake_run
    result = agent_host.launch(
        _wake(), inventory, runner_session_id="run_bug139")

    assert result["runner_session_id"] == "run_bug139"
    assert captured["request"] == (
        "POST", agent_host.P_DIRECT_SESSION_MCP_TOKEN,
        {"project": "switchboard", "wake_id": "wake-bug139",
         "host_id": "host/bug139", "runner_session_id": "run_bug139"},
    )
    assert captured["env"]["SWITCHBOARD_CONNECT_SESSION_TOKEN"] == "dst-task-session"
    assert captured["env"]["SWITCHBOARD_CONNECT_SESSION_TOKEN"] != "narrow-host-token"
    # The configured MCP client reads bearer_token_env_var=PM_MCP_TOKEN: the
    # minted task principal must override the inherited narrow host bearer or
    # the session still authenticates as the host (the BUG-139 symptom).
    assert captured["env"]["PM_MCP_TOKEN"] == "dst-task-session"
    assert "SWITCHBOARD_TOKEN" not in captured["env"]
    child = captured["command"][captured["command"].index("--") + 1:]
    assert any("SWITCHBOARD_CONNECT_SESSION_TOKEN" in arg for arg in child)
finally:
    agent_host.sb._http = saved_http
    agent_host.subprocess.run = saved_run
    if saved_host_token is None:
        os.environ.pop("PM_MCP_TOKEN", None)
    else:
        os.environ["PM_MCP_TOKEN"] = saved_host_token

print("BUG-139 Connect session token: passed")
