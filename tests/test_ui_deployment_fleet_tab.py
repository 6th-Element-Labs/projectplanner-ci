"""Fleet dock deployment tab and guarded agent-dispatch request."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import deployment_status  # noqa: E402
from switchboard.api.routers import board  # noqa: E402


RUNNING = "a" * 40
OLD = "b" * 40
MERGED = "c" * 40


def _pull(number: int, sha: str, merged_at: str) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.example/pull/{number}",
        "merged_at": merged_at,
        "merge_commit_sha": sha,
        "user": {"login": "agent"},
    }


def test_deployment_read_model_counts_only_unconfirmed_rows(monkeypatch):
    monkeypatch.setattr(deployment_status.store, "get_project_github_repo",
                        lambda _project: "org/repo")
    monkeypatch.setattr(deployment_status.store, "list_tasks",
                        lambda **_kwargs: [])
    payload = deployment_status.build_deployments(
        "switchboard",
        token="token",
        now=100,
        health_fn=lambda: {
            "running_sha": RUNNING,
            "canonical_sha": MERGED,
            "deploy_signal": "stale",
            "last_deploy_ok": True,
        },
        list_fn=lambda _repo, _token: [
            _pull(2, MERGED, "2026-07-24T01:00:00Z"),
            _pull(1, OLD, "2026-07-23T01:00:00Z"),
        ],
        commits_fn=lambda _repo, _sha, _token: [{"sha": RUNNING}, {"sha": OLD}],
    )
    assert payload["undeployed_count"] == 1
    assert [row["status"] for row in payload["deployments"]] == [
        "undeployed", "deployed"]


def test_one_sha_pinned_deploy_task_marks_all_pending_rows_queued(monkeypatch):
    monkeypatch.setattr(deployment_status.store, "get_project_github_repo",
                        lambda _project: "org/repo")
    monkeypatch.setattr(deployment_status.store, "list_tasks", lambda **_kwargs: [{
        "task_id": "DEPLOY-4",
        "title": f"[deploy {MERGED[:12]}] Deploy canonical master",
        "status": "Not Started",
    }])
    payload = deployment_status.build_deployments(
        "switchboard",
        token="token",
        health_fn=lambda: {
            "running_sha": RUNNING,
            "canonical_sha": MERGED,
            "deploy_signal": "stale",
            "last_deploy_ok": True,
        },
        list_fn=lambda _repo, _token: [
            _pull(3, MERGED, "2026-07-24T02:00:00Z"),
            _pull(2, "d" * 40, "2026-07-24T01:00:00Z"),
        ],
        commits_fn=lambda _repo, _sha, _token: [{"sha": RUNNING}],
    )
    assert payload["undeployed_count"] == 2
    assert {row["status"] for row in payload["deployments"]} == {"queued"}
    assert {row["deploy_task_id"] for row in payload["deployments"]} == {"DEPLOY-4"}


def test_deploy_request_requires_system_authority_and_dispatches_agent(monkeypatch):
    snapshot = {
        "repo": "org/repo",
        "production": {"canonical_sha": MERGED},
        "deployments": [{
            "number": 7, "deployed": False, "merge_sha": MERGED,
        }],
    }
    monkeypatch.setattr(deployment_status, "build_deployments",
                        lambda _project: snapshot)
    monkeypatch.setattr(board.store, "list_tasks", lambda **_kwargs: [])
    monkeypatch.setattr(
        board.create_task_command, "execute_mapping_result",
        lambda *_args, **_kwargs: {"task_id": "DEPLOY-1", "status": "Not Started"})
    monkeypatch.setattr(
        board.task_execution_command, "execute_mapping_result",
        lambda *_args, **_kwargs: {
            "action": "started", "started": True, "wake_id": "wake-1"})
    seen = {}

    def principal(_request, project, scopes, **_kwargs):
        seen["project"] = project
        seen["scopes"] = scopes
        return {"id": "user-1", "actor": "operator"}

    app = FastAPI()
    app.include_router(board.create_router(
        resolve_project=lambda value: value,
        resolve_principal=principal,
        etag_json=lambda *_args, **_kwargs: None,
        saturation_snapshot=lambda _project: {},
        sibling_bc_only=True,
    ))
    response = TestClient(app).post(
        "/api/deployments/request",
        json={"project": "switchboard", "pr_number": 7},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["task_id"] == "DEPLOY-1"
    assert seen == {"project": "switchboard", "scopes": ("write:system",)}


def test_fleet_dock_has_third_tab_count_and_deploy_action():
    source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "tabBtn('deployments', 'Deployments', undeployed)" in source
    assert "data-deploy-pr=" in source
    assert "/api/deployments/request" in source
    assert "Deploy current canonical master to production?" in source
