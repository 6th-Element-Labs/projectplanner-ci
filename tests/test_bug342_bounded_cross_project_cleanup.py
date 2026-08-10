#!/usr/bin/env python3
"""BUG-342: stale receipt retries cannot starve live host presence."""
from __future__ import annotations

import json
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


projects = [f"project-{index}" for index in range(9)]
inventory = {
    "host_id": "host/bug342",
    "placement": {"projects": projects},
}

calls = []
saved_project = agent_host.PROJECT
saved_drain = agent_host._drain_runners
try:
    agent_host.PROJECT = projects[0]

    def fake_drain(_host_id, *args, **kwargs):
        project = kwargs.get("project", agent_host.PROJECT)
        calls.append(project)
        rows = [{
            "runner_session_id": f"live-{project}",
            "task_id": f"LIVE-{project}",
            "alive": True,
            "status": "running",
        }]
        rows.append({
            "runner_session_id": f"stale-{project}",
            "task_id": f"STALE-{project}",
            "alive": False,
            "status": "running",
            "_pending_completion": True,
        })
        return rows

    agent_host._drain_runners = fake_drain
    first = agent_host._drain_runner_projects(inventory)
    first_calls = list(calls)
    calls.clear()
    second = agent_host._drain_runner_projects(inventory)
    second_calls = list(calls)
finally:
    agent_host.PROJECT = saved_project
    agent_host._drain_runners = saved_drain


def pending(rows):
    return [row for row in rows if row.get("_pending_completion")]


assert len(first_calls) == len(projects), first_calls
assert len(second_calls) == len(projects), second_calls
assert len(pending(first)) == 4, pending(first)
assert len(pending(second)) == 4, pending(second)
assert len([row for row in first if row["runner_session_id"].startswith("live-")]) == 9
assert len([row for row in second if row["runner_session_id"].startswith("live-")]) == 9

# The retry window rotates, so an unrepairable old row on one project cannot
# permanently starve completion receipts owned by later projects.
first_retry_projects = {
    row["_host_project"] for row in pending(first)
}
second_retry_projects = {
    row["_host_project"] for row in pending(second)
}
assert first_retry_projects != second_retry_projects, (
    first_retry_projects, second_retry_projects)

# Bound the server page too. The global selector above caps repair POSTs; this
# query cap keeps a large historical result from consuming the tick first.
paths = []
saved_run = agent_host.subprocess.run
saved_try = agent_host._try
try:
    agent_host.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"sessions": []}), stderr="")

    def fake_try(method, path, body=None):
        del body
        if method == "GET":
            paths.append(path)
        return {"sessions": []}

    agent_host._try = fake_try
    agent_host._drain_runners("host/bug342", project=projects[0])
finally:
    agent_host.subprocess.run = saved_run
    agent_host._try = saved_try

pending_paths = [path for path in paths if "pending_completion=true" in path]
assert len(pending_paths) == 1, pending_paths
assert "limit=1" in pending_paths[0], pending_paths[0]

print("BUG-342 bounded cross-project cleanup: PASS")
