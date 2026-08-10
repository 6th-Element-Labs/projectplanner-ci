#!/usr/bin/env python3
"""BUG-287: operator review recovery derives only the persisted PR head."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import connect_dispatch, task_execution
from switchboard.storage.repositories import (
    project_execution_policy,
    project_execution_readiness,
    projects as projects_repo,
)


PROJECT = "switchboard"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
captured: list[dict] = []

saved_request = connect_dispatch.coordination_repo.request_wake
saved_projection = task_execution._projection
saved_live_executions = task_execution.runner_repo.task_live_executions
saved_session_profile = task_execution.work_sessions_repo._task_work_session_profile
saved_readiness = project_execution_readiness.get_project_execution_readiness
saved_policy = project_execution_policy.get_project_execution_policy
saved_resolve = connect_dispatch.execution_context.resolve
saved_topology = projects_repo.get_project_repo_topology


def projection(head: str) -> dict:
    return {
        "task": {
            "task_id": "BUG-287",
            "_wsId": "BUG",
            "git_state": {
                "head_sha": head,
                "branch": "agent/switchboard/BUG-287/existing-pr",
                "pr_number": 287,
                "pr_url": "https://github.com/example/projectplanner/pull/287",
            },
        },
    }


def request_wake(**kwargs):
    captured.append(kwargs)
    return {"wake_id": f"wake-bug287-{len(captured)}", "status": "pending"}


def assert_exact_head_refusal(callable_) -> None:
    try:
        callable_()
    except task_execution.TaskExecutionError as exc:
        assert exc.code == "start_refused", exc.as_dict()
        assert (
            exc.details["start_error"] == "execution_checkout_head_mismatch"
        ), exc.as_dict()
    else:
        raise AssertionError("review recovery started without one persisted exact head")


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
    connect_dispatch.coordination_repo.request_wake = request_wake
    task_execution.runner_repo.task_live_executions = lambda *_args, **_kwargs: []
    task_execution.work_sessions_repo._task_work_session_profile = (
        lambda *_args, **_kwargs: "code_strict"
    )
    task_execution._projection = lambda *_args, **_kwargs: projection(HEAD)

    started = task_execution.start_task(
        "BUG-287", project=PROJECT, actor="operator",
        role="review_merge", source_sha="",
    )
    retried = task_execution.retry_task(
        "BUG-287", project=PROJECT, actor="operator",
        role="remediation", source_sha="",
    )

    assert started["started"] is True, started
    assert retried["started"] is True, retried
    assert len(captured) == 2, captured
    assert [
        row["policy"]["lifecycle"]["head_sha"] for row in captured
    ] == [HEAD, HEAD]
    assert all(row["policy"]["repository_binding"]["repository"]
               == "6th-Element-Labs/projectplanner" for row in captured)

    task_execution._projection = lambda *_args, **_kwargs: projection("")
    assert_exact_head_refusal(lambda: task_execution.start_task(
        "BUG-287", project=PROJECT, actor="operator",
        role="review_merge", source_sha="",
    ))

    task_execution._projection = lambda *_args, **_kwargs: projection(HEAD)
    assert_exact_head_refusal(lambda: task_execution.start_task(
        "BUG-287", project=PROJECT, actor="operator",
        role="remediation", source_sha=OTHER_HEAD,
    ))
finally:
    connect_dispatch.coordination_repo.request_wake = saved_request
    task_execution._projection = saved_projection
    task_execution.runner_repo.task_live_executions = saved_live_executions
    task_execution.work_sessions_repo._task_work_session_profile = saved_session_profile
    project_execution_readiness.get_project_execution_readiness = saved_readiness
    project_execution_policy.get_project_execution_policy = saved_policy
    connect_dispatch.execution_context.resolve = saved_resolve
    projects_repo.get_project_repo_topology = saved_topology


print("BUG-287 operator recovery exact-head derivation: PASS")
