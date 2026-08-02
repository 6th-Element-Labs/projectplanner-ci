#!/usr/bin/env python3
"""COORD-111: isolated v4 writer without changing the production v1 graph."""
from __future__ import annotations

from unittest.mock import patch

from path_setup import ROOT as _ROOT
from coordinator_daemon import DaemonConfig
from switchboard.application.mission_bot_v4.coordinator import (
    V4ScopedCompletionCoordinator,
)
from switchboard.connect.contract import Ack, Assignment, ResourceLimits
from switchboard.connect.execution_assignment import build_execution_assignment
from switchboard.connect.launcher import assignment_note


def test_v4_isolated_edge_accepts_forwarded_hosts_on_loopback_only():
    config = (_ROOT / "deploy" / "Caddyfile.v4-isolated-test").read_text(
        encoding="utf-8"
    )

    assert "http://:8110 {" in config
    assert "\tbind 127.0.0.1" in config
    assert "http://127.0.0.1:8110 {" not in config


def test_v4_test_owner_calls_only_the_scoped_v4_runtime():
    authority = {
        "schema": "switchboard.autopilot_scope_authority.v1",
        "scope_id": "scope-coord111-test",
        "lease_id": "lease-coord111-test",
        "holder_agent_id": "codex/COORD-111-test",
        "generation": 1,
        "fence_epoch": 1,
    }
    owner = V4ScopedCompletionCoordinator(
        DaemonConfig(projects=("switchboard",), act=True),
        store_mod=object(),
        agent_id="codex/COORD-111-test",
    )
    expected = {"schema": "switchboard.mission_bot_v4.tick.v1", "action": "wait"}
    with (
        patch(
            "switchboard.application.mission_bot_v4.run_scoped_mission_tick",
            return_value=expected,
        ) as v4,
        patch(
            "switchboard.application.completion_driver.run_completion_tick"
        ) as v1,
    ):
        actual = owner._completion_tick(
            "QA-119",
            task_project="switchboard",
            scope_project="switchboard",
            authority=authority,
        )

    assert actual is expected
    assert v4.call_count == 1
    assert v4.call_args.kwargs["scope_authority"] == authority
    assert v1.call_count == 0


def test_production_owner_still_calls_only_v1():
    from scoped_completion_coordinator import ScopedCompletionCoordinator

    authority = {"scope_id": "scope-v1"}
    owner = ScopedCompletionCoordinator(
        DaemonConfig(projects=("switchboard",), act=True),
        store_mod=object(),
        agent_id="codex/v1",
    )
    expected = {"schema": "switchboard.completion_tick.v1"}
    with (
        patch(
            "switchboard.application.completion_driver.run_completion_tick",
            return_value=expected,
        ) as v1,
        patch(
            "switchboard.application.mission_bot_v4.run_scoped_mission_tick"
        ) as v4,
    ):
        actual = owner._completion_tick(
            "QA-118",
            task_project="switchboard",
            scope_project="switchboard",
            authority=authority,
        )

    assert actual is expected
    assert v1.call_count == 1
    assert v4.call_count == 0


class TerminalScopeStore:
    def __init__(self):
        self.updates = []

    @staticmethod
    def get_task(task_id, *, project):
        assert task_id == "QA-120"
        assert project == "switchboard"
        return {
            "task_id": task_id,
            "status": "Done",
            "provenance": {"terminal": True},
        }

    def update_autopilot_scope(self, scope_id, **kwargs):
        self.updates.append((scope_id, kwargs))
        return {"scope_id": scope_id, **kwargs}

    @staticmethod
    def heartbeat(*_args, **_kwargs):
        return {"ok": True}

    @staticmethod
    def acquire_autopilot_scope_lease(scope_id, **_kwargs):
        return {"scope_id": scope_id, "generation": 3, "fence_epoch": 3}


def test_v4_projects_terminal_provenance_before_completing_task_scope():
    store = TerminalScopeStore()
    owner = V4ScopedCompletionCoordinator(
        DaemonConfig(projects=("switchboard",), act=True),
        store_mod=store,
        agent_id="codex/COORD-111-test",
        clock=lambda: 123.0,
    )
    scope = {
        "scope_id": "scope-coord111-terminal",
        "scope_type": "task",
        "task_project": "switchboard",
        "task_id": "QA-120",
    }
    authority = {"scope_id": scope["scope_id"], "generation": 2}
    with patch.object(
        owner,
        "_completion_tick",
        return_value={
            "schema": "switchboard.mission_worker_tick.v4",
            "task_id": "QA-120",
            "action": "wait",
            "reason": "terminal_provenance",
        },
    ) as tick:
        result = owner._run_standalone_task_scope(
            "switchboard", scope, authority,
        )

    assert result["status"] == "completed"
    assert result["receipts"][0]["reason"] == "terminal_provenance"
    assert store.updates[-1][1]["status"] == "completed"
    tick.assert_called_once_with(
        "QA-120",
        task_project="switchboard",
        scope_project="switchboard",
        authority=authority,
    )


def test_v4_keeps_scope_active_when_terminal_projection_is_not_confirmed():
    store = TerminalScopeStore()
    owner = V4ScopedCompletionCoordinator(
        DaemonConfig(projects=("switchboard",), act=True),
        store_mod=store,
        agent_id="codex/COORD-111-test",
        clock=lambda: 123.0,
    )
    scope = {
        "scope_id": "scope-coord111-terminal",
        "scope_type": "task",
        "task_project": "switchboard",
        "task_id": "QA-120",
    }
    with patch.object(
        owner,
        "_completion_tick",
        return_value={
            "schema": "switchboard.mission_worker_tick.v4",
            "task_id": "QA-120",
            "action": "block_release",
            "reason": "canonical_terminal_provenance_missing",
            "release_blocked": True,
        },
    ):
        result = owner._run_standalone_task_scope(
            "switchboard", scope, {"scope_id": scope["scope_id"]},
        )

    assert result["status"] == "completion_tick_failed"
    assert result["release_blocked"] is True
    assert "status" not in store.updates[-1][1]


def test_v4_projects_terminal_provenance_before_completing_deliverable_scope():
    store = TerminalScopeStore()
    store.get_mission_status = lambda **_kwargs: {
        "deliverable": {"status": "in_review"},
        "dispatch_scope": {"links": [{
            "task_id": "QA-120",
            "project_id": "switchboard",
            "automatic_dispatch_eligible": True,
        }]},
        "linked_tasks": [{
            "task_id": "QA-120",
            "project_id": "switchboard",
            "task_detail": {
                "status": "Done",
                "provenance": {"terminal": True},
            },
        }],
    }
    owner = V4ScopedCompletionCoordinator(
        DaemonConfig(projects=("switchboard",), act=True),
        store_mod=store,
        agent_id="codex/COORD-111-test",
        clock=lambda: 123.0,
    )
    scope = {
        "scope_id": "scope-coord111-deliverable",
        "scope_type": "deliverable",
        "deliverable_id": "QA-V4-REPLAY",
    }
    with patch.object(
        owner,
        "_completion_tick",
        return_value={
            "schema": "switchboard.mission_worker_tick.v4",
            "task_id": "QA-120",
            "action": "wait",
            "reason": "terminal_provenance",
        },
    ) as tick:
        result = owner.run_scope("switchboard", scope)

    assert result["status"] == "completed"
    assert store.updates[-1][1]["status"] == "completed"
    tick.assert_called_once()


def test_v4_assignment_uses_the_journal_yield_not_the_legacy_factory():
    contract = build_execution_assignment(
        task_id="QA-119",
        assignment={"assignment_id": "assignment-coord111"},
        lifecycle={
            "role": "implementation",
            "execution_id": "exec-coord111",
            "generation": 2,
            "head_sha": "",
            "mission_key": "v4:2:QA-119:1:implementation",
        },
    )
    assert contract["typed_tools"]["mission_context"] == "get_mission_context"
    assert contract["typed_tools"]["mission_yield"] == "yield_mission"
    assert "stale_assignment" not in contract["typed_tools"]

    v1_contract = build_execution_assignment(
        task_id="QA-118",
        assignment={"assignment_id": "assignment-v1"},
        lifecycle={
            "role": "implementation",
            "execution_id": "exec-v1",
            "generation": 2,
            "head_sha": "",
            "mission_key": "legacy-completion-idempotency-key",
        },
    )
    assert v1_contract["typed_tools"]["stale_assignment"] == (
        "report_stale_assignment"
    )
    assert "mission_yield" not in v1_contract["typed_tools"]

    note = assignment_note(
        Ack(
            lease_id="lease-coord111",
            runner_id="runner-coord111",
            assignment=Assignment(
                assignment_id="assignment-coord111",
                work_ref="task:switchboard:QA-119",
                workspace_ref="repo:canonical@base",
                runtime="codex",
                provider="openai",
                principal_ref="agent/codex/qa-119",
                limits=ResourceLimits(max_runtime_seconds=3600),
                queued_at=1,
            ),
            host_id="host/coord111",
            issued_at=1,
            expires_at=2,
            heartbeat_interval_seconds=30,
            last_heartbeat_at=1,
        ),
        contract,
    )
    assert "call yield_mission for this exact execution_id and generation" in note
    assert "do not call report_stale_assignment" in note
    assert "never use the fresh workspace or base-branch HEAD" in note
    assert "yield outcome=waiting" in note
    assert "If no live PR exists for the task" in note
    assert "publish the untouched branch" in note
    assert "before preflight and claim" in note
    assert "use complete_claim for the existing ADR-0008 C3" in note
    assert "Do not yield merely because the new task has no PR" in note

    review_contract = build_execution_assignment(
        task_id="QA-119",
        assignment={"assignment_id": "assignment-coord111-review"},
        lifecycle={
            "role": "review_merge",
            "execution_id": "exec-coord111-review",
            "generation": 3,
            "head_sha": "a" * 40,
            "pr_number": 119,
            "pr_url": "https://github.com/example/repo/pull/119",
            "mission_key": "v4:2:QA-119:2:review_merge",
        },
    )
    review_note = assignment_note(
        Ack(
            lease_id="lease-coord111-review",
            runner_id="runner-coord111-review",
            assignment=Assignment(
                assignment_id="assignment-coord111-review",
                work_ref="task:switchboard:QA-119",
                workspace_ref="repo:canonical@base",
                runtime="codex",
                provider="openai",
                principal_ref="agent/codex/qa-119",
                limits=ResourceLimits(max_runtime_seconds=3600),
                queued_at=1,
            ),
            host_id="host/coord111",
            issued_at=1,
            expires_at=2,
            heartbeat_interval_seconds=30,
            last_heartbeat_at=1,
        ),
        review_contract,
    )
    assert "do not yield before doing the assigned role" in review_note
    assert "perform review/merge or remediation" in review_note
    assert "the role boundary is yield_mission itself" in review_note
    assert "requested_role=remediation" in review_note
    assert "Do not call abandon_claim or complete_claim before that yield" in review_note
    assert "call reconcile_task_merge for this exact task" in review_note
    assert "Do not call yield_mission after merge" in review_note
    assert "page a redundant reviewer" in review_note
    assert "does not let the agent declare Done" in review_note


if __name__ == "__main__":
    test_v4_isolated_edge_accepts_forwarded_hosts_on_loopback_only()
    test_v4_test_owner_calls_only_the_scoped_v4_runtime()
    test_production_owner_still_calls_only_v1()
    test_v4_projects_terminal_provenance_before_completing_task_scope()
    test_v4_keeps_scope_active_when_terminal_projection_is_not_confirmed()
    test_v4_projects_terminal_provenance_before_completing_deliverable_scope()
    test_v4_assignment_uses_the_journal_yield_not_the_legacy_factory()
    print("COORD-111 isolated v4 writer: 7 passed")
