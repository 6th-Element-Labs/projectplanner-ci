#!/usr/bin/env python3
"""WATCH-16: review stewardship launches one Connect generation per PR head."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import connect_dispatch, task_execution


captured: list[dict] = []
saved_request = connect_dispatch.coordination_repo.request_wake
saved_projection = task_execution._projection
saved_live_executions = task_execution.runner_repo.task_live_executions


def request_wake(**kwargs):
    captured.append(kwargs)
    return {"wake_id": "wake-review", "status": "pending"}


try:
    connect_dispatch.coordination_repo.request_wake = request_wake
    task_execution.runner_repo.task_live_executions = lambda *_args, **_kwargs: []
    task_execution._projection = lambda *_args, **_kwargs: {
        "task": {"task_id": "WATCH-16", "_wsId": "WATCH", "updated_at": 12.0},
    }
    first = task_execution.start_task(
        "WATCH-16", project="switchboard", actor="review-steward",
        role="review_merge", source_sha="a" * 40,
        instruction="Review the PR and merge through the queue if green.")
    # Unrelated task activity must not alter the request payload for this head.
    task_execution._projection = lambda *_args, **_kwargs: {
        "task": {"task_id": "WATCH-16", "_wsId": "WATCH", "updated_at": 99.0},
    }
    task_execution.start_task(
        "WATCH-16", project="switchboard", actor="review-steward",
        role="review_merge", source_sha="a" * 40)
    task_execution.start_task(
        "WATCH-16", project="switchboard", actor="review-steward",
        role="review_merge", source_sha="b" * 40)
    retried = task_execution.retry_task(
        "WATCH-16", project="switchboard", actor="review-steward",
        role="review_merge", source_sha="c" * 40,
        instruction="Review the replacement exact head.")
finally:
    connect_dispatch.coordination_repo.request_wake = saved_request
    task_execution._projection = saved_projection
    task_execution.runner_repo.task_live_executions = saved_live_executions

assert first["started"] is True and first["role"] == "review_merge", first
assert retried["started"] is True, retried
assert len(captured) == 4
assert captured[0]["policy"]["mode"] == "connect"
assert set(captured[0]["policy"]) == {
    "mode", "assignment", "lifecycle", "effect_identity"}
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
