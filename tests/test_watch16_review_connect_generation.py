#!/usr/bin/env python3
"""WATCH-16: review stewardship launches one Connect generation per PR head."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import connect_dispatch, task_execution
from switchboard.storage.repositories import (
    project_execution_policy,
    project_execution_readiness,
    projects as projects_repo,
)


captured: list[dict] = []
saved_request = connect_dispatch.coordination_repo.request_wake
saved_projection = task_execution._projection
saved_live_executions = task_execution.runner_repo.task_live_executions
saved_readiness = project_execution_readiness.get_project_execution_readiness
saved_policy = project_execution_policy.get_project_execution_policy
saved_resolve = connect_dispatch.execution_context.resolve
saved_topology = projects_repo.get_project_repo_topology
saved_profile = task_execution.work_sessions_repo._task_work_session_profile


def request_wake(**kwargs):
    captured.append(kwargs)
    return {"wake_id": "wake-review", "status": "pending"}


try:
    project_execution_readiness.get_project_execution_readiness = (
        lambda _project: {"passed": True}
    )
    project_execution_policy.get_project_execution_policy = (
        lambda _project: {"configured": True, "activated": True}
    )
    connect_dispatch.execution_context.resolve = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("policy-free Connect must not resolve execution context"))
    projects_repo.get_project_repo_topology = lambda _project: {
        "roles": {"canonical": {
            "configured": True,
            "repo": "6th-Element-Labs/projectplanner",
            "default_branch": "master",
        }}
    }
    task_execution.work_sessions_repo._task_work_session_profile = (
        lambda *_args, **_kwargs: "")
    connect_dispatch.coordination_repo.request_wake = request_wake
    task_execution.runner_repo.task_live_executions = lambda *_args, **_kwargs: []
    def projection(head, updated_at):
        return {
            "task": {
                "task_id": "WATCH-16", "_wsId": "WATCH",
                "updated_at": updated_at,
                "git_state": {
                    "head_sha": head,
                    "branch": "agent/switchboard/WATCH-16/existing-pr",
                },
            },
        }

    task_execution._projection = lambda *_args, **_kwargs: projection("a" * 40, 12.0)
    first = task_execution.start_task(
        "WATCH-16", project="switchboard", actor="review-steward",
        role="review_merge", source_sha="a" * 40,
        instruction="Review the PR and merge through the queue if green.")
    # Unrelated task activity must not alter the request payload for this head.
    task_execution._projection = lambda *_args, **_kwargs: projection("a" * 40, 99.0)
    task_execution.start_task(
        "WATCH-16", project="switchboard", actor="review-steward",
        role="review_merge", source_sha="a" * 40)
    task_execution._projection = lambda *_args, **_kwargs: projection("b" * 40, 100.0)
    task_execution.start_task(
        "WATCH-16", project="switchboard", actor="review-steward",
        role="review_merge", source_sha="b" * 40)
    task_execution._projection = lambda *_args, **_kwargs: projection("c" * 40, 101.0)
    retried = task_execution.retry_task(
        "WATCH-16", project="switchboard", actor="review-steward",
        role="review_merge", source_sha="c" * 40,
        instruction="Review the replacement exact head.")
finally:
    connect_dispatch.coordination_repo.request_wake = saved_request
    task_execution._projection = saved_projection
    task_execution.runner_repo.task_live_executions = saved_live_executions
    project_execution_readiness.get_project_execution_readiness = saved_readiness
    project_execution_policy.get_project_execution_policy = saved_policy
    connect_dispatch.execution_context.resolve = saved_resolve
    projects_repo.get_project_repo_topology = saved_topology
    task_execution.work_sessions_repo._task_work_session_profile = saved_profile

assert first["started"] is True and first["role"] == "review_merge", first
assert retried["started"] is True, retried
assert len(captured) == 4
assert captured[0]["policy"]["mode"] == "connect"
# BUG-345 keeps launch policy-free. The exact set pins the small wake contract.
assert set(captured[0]["policy"]) == {
    "mode", "assignment", "lifecycle", "effect_identity",
    "repository_binding", "coordination_scope"}
assert captured[0]["policy"]["coordination_scope"] == {
    "schema": "switchboard.scoped_start_request.v1",
    "scope_type": "task",
    "task_project": "switchboard",
    "task_id": "WATCH-16",
    "runtime": "codex",
}
assert captured[0]["idem_key"] == captured[1]["idem_key"]
assert captured[0]["policy"] == captured[1]["policy"]
assert captured[2]["idem_key"] != captured[0]["idem_key"]
assert captured[3]["idem_key"] != captured[2]["idem_key"]
assert (captured[2]["policy"]["assignment"]["assignment_id"]
        != captured[0]["policy"]["assignment"]["assignment_id"])
assert (captured[3]["policy"]["assignment"]["assignment_id"]
        != captured[2]["policy"]["assignment"]["assignment_id"])
assert all(row["policy"]["lifecycle"]["role"] == "review_merge"
           and "source_sha" not in row["policy"]["assignment"]
           for row in captured)

print("WATCH-16 review Connect generation: PASS")
