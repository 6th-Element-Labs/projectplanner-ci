from __future__ import annotations

from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

import open_prs
from switchboard.application.queries import completion_projection


HEAD = "a" * 40


def _run(route: str, *, board_status: str, reason: str,
         state: str = "blocked", role: str = "") -> dict:
    return {
        "task_id": "UI-62",
        "pr_number": 810,
        "head_sha": HEAD,
        "state": state,
        "route": route,
        "reason_code": reason,
        "desired_role": role,
        "next_retry_at": 1_800_000_000,
        "board_status": board_status,
        "attempt": 2,
        "state_version": 7,
        "evidence_refs": {"decision": {"effect": f"effect_{route}"}},
    }


def test_remediation_and_coordination_retry_have_distinct_board_projection():
    remediation = completion_projection.project_completion(
        _run("remediation", board_status="Blocked",
             reason="required_ci_failed", role="remediation"))
    retry = completion_projection.project_completion(
        _run("coordination_retry", board_status="In Review",
             reason="review_merge_start_failed", role="review_merge"))

    assert remediation == {
        **remediation,
        "route": "remediation",
        "route_owner": "remediation agent",
        "board_status": "Blocked",
        "desired_role": "remediation",
        "desired_head": HEAD,
        "current_effect": "effect_remediation",
    }
    assert retry["route"] == "coordination_retry"
    assert retry["route_owner"] == "coordinator"
    assert retry["board_status"] == "In Review"
    assert retry["desired_role"] == "review_merge"


def test_canonical_merged_sha_is_the_only_done_projection():
    projected = completion_projection.project_completion(
        _run("reconcile", board_status="In Review",
             reason="merged_pending_reconcile", state="reconciling"),
        task={"task_id": "UI-62", "status": "In Review",
              "git_state": {"merged_sha": "b" * 40}})

    assert projected["terminal"] is True
    assert projected["state"] == "done"
    assert projected["route"] == "none"
    assert projected["board_status"] == "Done"
    assert projected["merged_sha"] == "b" * 40


def test_board_batch_projection_uses_one_completion_query():
    tasks = [
        {"task_id": "A-1", "status": "Blocked"},
        {"task_id": "A-2", "status": "In Review"},
    ]
    runs = {
        "A-1": {**_run("remediation", board_status="Blocked",
                       reason="required_ci_failed"), "task_id": "A-1"},
        "A-2": {**_run("coordination_retry", board_status="In Review",
                       reason="start_failed"), "task_id": "A-2"},
    }
    with patch.object(
            completion_projection.completion_runs,
            "list_active_completion_runs", return_value=runs) as query:
        completion_projection.attach_many(tasks, project="switchboard")

    query.assert_called_once()
    assert tasks[0]["completion_projection"]["route"] == "remediation"
    assert tasks[1]["completion_projection"]["route"] == "coordination_retry"


def test_pr_join_carries_same_projection_and_draft_does_not_mask_red_ci():
    projection = completion_projection.project_completion(
        _run("remediation", board_status="Blocked",
             reason="required_ci_failed", role="remediation"))
    row = {
        "draft": True,
        "ci_state": "failure",
        "ci_failing": ["Switchboard CI / VM gate"],
        "mergeable_state": "blocked",
        "completion_projection": projection,
    }
    assert open_prs.classify(row) == {
        "blocked": True,
        "blocked_reason": "Switchboard CI / VM gate failed",
    }

    joined = open_prs._board_join(
        {"title": "UI-62: completion projection",
         "head": {"ref": "codex/UI-62-completion-projection"}},
        "switchboard",
        lambda task_id, project="": {
            "task_id": task_id,
            "status": "Blocked",
            "completion_projection": projection,
        })
    assert joined["completion_projection"] == projection
    assert joined["tasks"][0]["completion_projection"] == projection


def test_pr_join_hydrates_projection_for_raw_repository_task():
    run = _run("coordination_retry", board_status="In Review",
               reason="review_merge_start_failed", role="review_merge")
    with patch.object(
            completion_projection.completion_runs,
            "get_active_completion_run", return_value=run) as query:
        joined = open_prs._board_join(
            {"title": "UI-62: completion projection",
             "head": {"ref": "codex/UI-62-completion-projection"}},
            "switchboard",
            lambda task_id, project="": {
                "task_id": task_id, "status": "In Review",
            })

    query.assert_called_once_with("UI-62", project="switchboard")
    assert joined["completion_projection"]["route"] == "coordination_retry"
