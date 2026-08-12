#!/usr/bin/env python3
"""BUG-348: one project's receipt backlog cannot starve another project."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
from adapters import agent_host


HOST = "host/bug348"
PROJECTS = ["backlogged", "fresh"]


def pending(project: str, index: int) -> dict:
    return {
        "runner_session_id": f"run-{project}-{index}",
        "task_id": f"TASK-{project}-{index}",
        "_pending_completion": True,
    }


saved_project = agent_host.PROJECT
saved_cursor = agent_host._PENDING_COMPLETION_PROJECT_CURSOR
saved_drain = agent_host._drain_runners
try:
    agent_host.PROJECT = PROJECTS[0]
    agent_host._PENDING_COMPLETION_PROJECT_CURSOR = 0

    def fake_drain(_host_id, *args, project=None, **kwargs):
        del args, kwargs
        project_id = str(project or agent_host.PROJECT)
        if project_id == PROJECTS[0]:
            return [pending(project_id, index) for index in range(8)]
        return [
            {
                "runner_session_id": "run-fresh-live",
                "task_id": "TASK-fresh-live",
                "alive": True,
            },
            pending(project_id, 0),
        ]

    agent_host._drain_runners = fake_drain
    rows = agent_host._drain_runner_projects({
        "host_id": HOST,
        "placement": {"projects": PROJECTS},
    })
finally:
    agent_host.PROJECT = saved_project
    agent_host._PENDING_COMPLETION_PROJECT_CURSOR = saved_cursor
    agent_host._drain_runners = saved_drain


pending_rows = [row for row in rows if row.get("_pending_completion") is True]
assert len(pending_rows) == agent_host._PENDING_COMPLETION_RETRIES_PER_TICK, rows
assert "run-fresh-0" in {
    row["runner_session_id"] for row in pending_rows
}, pending_rows
assert any(row["runner_session_id"] == "run-fresh-live" for row in rows), rows

print("BUG-348 fair pending completion drain: PASS")
