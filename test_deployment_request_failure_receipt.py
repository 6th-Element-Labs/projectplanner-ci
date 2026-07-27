#!/usr/bin/env python3
"""Deploy-button start refusals stay visible and retryable."""
from __future__ import annotations

import os
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deployment_status  # noqa: E402
from switchboard.api.routers import board  # noqa: E402


RUNNING = "a" * 40
MERGED = "c" * 40
recorded = {}


def pull(number: int, sha: str) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.example/pull/{number}",
        "merged_at": "2026-07-24T02:00:00Z",
        "merge_commit_sha": sha,
        "user": {"login": "agent"},
    }


original_list_tasks = deployment_status.store.list_tasks
try:
    deployment_status.store.list_tasks = lambda **_kwargs: [{
        "task_id": "DEPLOY-4",
        "title": f"[deploy {MERGED[:12]}] Deploy canonical master",
        "status": "Blocked",
    }]
    payload = deployment_status.build_deployments(
        "switchboard",
        repo="org/repo",
        token="token",
        health_fn=lambda: {
            "running_sha": RUNNING,
            "canonical_sha": MERGED,
            "deploy_signal": "stale",
            "last_deploy_ok": True,
        },
        list_fn=lambda _repo, _token: [pull(3, MERGED)],
        commits_fn=lambda _repo, _sha, _token: [{"sha": RUNNING}],
        canonical_fn=lambda _repo, _ref, _token: {"sha": MERGED},
    )
finally:
    deployment_status.store.list_tasks = original_list_tasks

assert payload["deployments"][0]["status"] == "failed"
assert "Blocked" in deployment_status.TERMINAL_STATUSES

snapshot = {
    "repo": "org/repo",
    "canonical_sha": MERGED,
    "production": {"running_sha": RUNNING},
    "deployments": [{
        "number": 7, "deployed": False, "merge_sha": MERGED,
    }],
}
original_build = deployment_status.build_deployments
original_board_list = board.store.list_tasks
original_create = board.create_task_command.execute_mapping_result
original_start = board.task_execution_command.execute_mapping_result
original_comment = board.store.add_comment
original_update = board.update_task_command.execute_mapping_result


def add_comment(task_id, actor, text, **kwargs):
    recorded["comment"] = (task_id, actor, text, kwargs)


def update_task(task_id, fields, **kwargs):
    recorded["update"] = (task_id, fields, kwargs)
    return {"task_id": task_id, "status": "Blocked"}


try:
    deployment_status.build_deployments = lambda _project: snapshot
    board.store.list_tasks = lambda **_kwargs: []
    board.create_task_command.execute_mapping_result = (
        lambda *_args, **_kwargs: {
            "task_id": "DEPLOY-5", "status": "Not Started"})
    board.task_execution_command.execute_mapping_result = (
        lambda *_args, **_kwargs: {
            "error_code": "start_refused",
            "start_error": "project_execution_not_ready",
            "message": "Project execution readiness is blocked.",
        })
    board.store.add_comment = add_comment
    board.update_task_command.execute_mapping_result = update_task

    app = FastAPI()
    app.include_router(board.create_router(
        resolve_project=lambda value: value,
        resolve_principal=lambda *_args, **_kwargs: {
            "id": "user-1", "actor": "operator"},
        etag_json=lambda *_args, **_kwargs: None,
        saturation_snapshot=lambda _project: {},
        sibling_bc_only=True,
    ))
    response = TestClient(app).post(
        "/api/deployments/request",
        json={"project": "switchboard", "pr_number": 7},
    )
finally:
    deployment_status.build_deployments = original_build
    board.store.list_tasks = original_board_list
    board.create_task_command.execute_mapping_result = original_create
    board.task_execution_command.execute_mapping_result = original_start
    board.store.add_comment = original_comment
    board.update_task_command.execute_mapping_result = original_update

assert response.status_code == 409
assert response.json()["detail"]["deployment_task_status"] == "Blocked"
assert recorded["update"][1] == {"status": "Blocked"}
assert recorded["comment"][3]["kind"] == "deployment.start_refused"
assert "project_execution_not_ready" in recorded["comment"][2]
print("PASS deploy-button start refusals are visible and retryable")
