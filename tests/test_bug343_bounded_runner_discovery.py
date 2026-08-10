#!/usr/bin/env python3
"""BUG-343: project discovery is linear, never projects x local tasks."""
from __future__ import annotations

import json
import urllib.parse
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


projects = [f"project-{index}" for index in range(9)]
local_sessions = [{
    "runner_session_id": f"run-{index}",
    "task_id": f"TASK-{index}",
    "alive": True,
    "status": "running",
    "metadata": {"native_host_execution": True, "wake_id": f"wake-{index}"},
} for index in range(9)]
inventory = {
    "host_id": "host/bug343",
    "placement": {"projects": projects},
}

get_paths = []
saved_project = agent_host.PROJECT
saved_run = agent_host.subprocess.run
saved_try = agent_host._try
try:
    agent_host.PROJECT = projects[0]
    agent_host.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"sessions": local_sessions}),
        stderr="",
    )

    def fake_try(method, path, body=None):
        del body
        if method != "GET":
            return {}
        get_paths.append(path)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        project = query.get("project", [agent_host.PROJECT])[0]
        if query.get("pending_completion") == ["true"]:
            return {"sessions": []}
        if query.get("include_stale") == ["false"]:
            index = projects.index(project)
            row = dict(local_sessions[index])
            row["host_id"] = inventory["host_id"]
            return {"sessions": [row]}
        return {"sessions": []}

    agent_host._try = fake_try
    rows = agent_host._drain_runner_projects(inventory)
finally:
    agent_host.PROJECT = saved_project
    agent_host.subprocess.run = saved_run
    agent_host._try = saved_try

live_queries = [path for path in get_paths if "include_stale=false" in path]
recovery_queries = [
    path for path in get_paths
    if "include_stale=true" in path and "task_id=" in path
]
pending_queries = [path for path in get_paths if "pending_completion=true" in path]

assert len(get_paths) <= 3 * len(projects), len(get_paths)
assert len(live_queries) == len(projects), live_queries
assert len(recovery_queries) == len(projects), recovery_queries
assert len(pending_queries) == len(projects), pending_queries
assert len(rows) == len(projects), rows
assert {
    (row["runner_session_id"], row["_host_project"]) for row in rows
} == {
    (f"run-{index}", project) for index, project in enumerate(projects)
}, rows

print("BUG-343 bounded runner discovery: PASS")
