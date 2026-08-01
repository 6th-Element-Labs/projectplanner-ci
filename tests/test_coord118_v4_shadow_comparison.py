#!/usr/bin/env python3
"""COORD-118: v4 shadow compares every proposal and owns no effect."""
from __future__ import annotations

import inspect
import unittest

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.application.mission_bot_v4 import shadow
from switchboard.application.mission_bot_v4.shadow import (
    compare_shadow_decisions as _compare_shadow_decisions,
    run_shadow_batch as _run_shadow_batch,
    run_shadow_comparison as _run_shadow_comparison,
)


PROJECT = "switchboard"
HEAD = "a" * 40
AUTHORITY = {
    "schema": "switchboard.autopilot_scope_authority.v1",
    "scope_id": "scope-deliverable-118",
    "holder_agent_id": "switchboard/coordinator-autopilot/test",
    "lease_id": "lease-118",
    "generation": 4,
    "fence_epoch": 3,
    "expires_at": 9999999999.0,
}


def allowed_scope(authority=AUTHORITY, **_kwargs):
    return {"allowed": True, "authority": dict(authority)}


def compare_shadow_decisions(**kwargs):
    kwargs.setdefault("scope_verdict", allowed_scope())
    return _compare_shadow_decisions(**kwargs)


def run_shadow_comparison(*args, **kwargs):
    kwargs.setdefault("scope_authority", AUTHORITY)
    kwargs.setdefault("scope_validator", allowed_scope)
    return _run_shadow_comparison(*args, **kwargs)


def run_shadow_batch(*args, **kwargs):
    kwargs.setdefault("scope_authority", AUTHORITY)
    kwargs.setdefault("scope_validator", allowed_scope)
    return _run_shadow_batch(*args, **kwargs)


def snapshot(*, task_id="QA-118", runner=False, deps=True, scope="active", **updates):
    value = {
        "task_id": task_id,
        "snapshot_id": f"snapshot-{task_id}",
        "head_sha": HEAD,
        "board_status": "In Review",
        "task": {
            "task_id": task_id,
            "status": "In Review",
            "dependency_state": {"satisfied": deps},
            "git_state": {"head_sha": HEAD, "pr_number": 118},
        },
        "dependency_state": {"satisfied": deps},
        "autopilot_scope": {"status": scope},
        "runner": {"live": runner},
        "github_pr": {"number": 118, "state": "OPEN", "draft": False,
                      "head": {"sha": HEAD}},
        "pr_number": 118,
        "status_contexts": {
            "Switchboard CI / VM gate": {
                "context": "Switchboard CI / VM gate",
                "state": "SUCCESS",
            },
        },
        "required_status_contexts": ["Switchboard CI / VM gate"],
        "review": {},
        "merge_gate": {},
        "merge_queue": {},
        "merge_provenance": {},
        "source_observed_at": {"runner_sessions": 100.0, "task": 100.0},
    }
    value.update(updates)
    return value


def mission(*, state="ACTIVE", role="review_merge", handled=0, latest=1, **updates):
    value = {
        "state": state,
        "requested_role": role,
        "handled_through": handled,
        "latest_sequence": latest,
        "version": 3,
        "terminal_kind": "",
        "terminal_ref": "",
    }
    value.update(updates)
    return value


class Journal:
    def __init__(self, rows):
        self.rows = rows

    def get_item(self, task_id, *, project):
        assert project == PROJECT
        row = self.rows.get(task_id)
        return dict(row) if row is not None else None


class V4ShadowComparisonTest(unittest.TestCase):
    def test_shadow_module_has_no_work_driving_or_journal_mutation_port(self):
        source = inspect.getsource(shadow)
        self.assertNotIn("task_execution", source)
        self.assertNotIn("tick_scoped_mission", source)
        self.assertNotIn("run_mission_tick", source)
        self.assertNotIn("append_event(", source)
        self.assertNotIn("update_item(", source)
        self.assertNotIn("ensure_item(", source)

    def test_same_review_page_is_an_exact_match(self):
        row = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(),
            mission=mission(),
            observed_at=101.0,
        )
        self.assertEqual("START_REVIEW", row["v1"]["output"])
        self.assertEqual("start_task", row["v4"]["action"])
        self.assertEqual("match", row["comparison_class"])
        self.assertFalse(row["release_blocked"])
        self.assertFalse(row["effect_port_bound"])
        self.assertFalse(row["cutover_authorized"])
        self.assertEqual("runner_sessions", row["runner_liveness_source"])
        self.assertTrue(row["scope_authority_validated"])
        self.assertEqual("scope-deliverable-118", row["scope_id"])

    def test_exact_deliverable_scope_authority_is_required_not_inferred(self):
        missing = _compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(autopilot_scope={}),
            mission=mission(),
        )
        self.assertEqual("scope_authority_required", missing["comparison_reason"])
        self.assertTrue(missing["release_blocked"])

        seen = []

        def validate(authority, **kwargs):
            seen.append((dict(authority), dict(kwargs)))
            return {"allowed": True, "authority": dict(authority)}

        row = _run_shadow_comparison(
            "QA-118",
            project=PROJECT,
            actor="test",
            scope_project="switchboard",
            scope_authority=AUTHORITY,
            scope_validator=validate,
            journal=Journal({"QA-118": mission()}),
            hydrator=lambda *_args, **_kwargs: snapshot(autopilot_scope={}),
            recorder=lambda *_args, **_kwargs: {"recorded": True},
        )
        self.assertEqual("match", row["comparison_class"])
        self.assertEqual(1, len(seen))
        self.assertEqual("QA-118", seen[0][1]["task_id"])
        self.assertEqual(PROJECT, seen[0][1]["task_project"])

    def test_v1_provider_effect_maps_to_v4_review_page(self):
        row = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(
                github_pr={"number": 118, "state": "OPEN", "draft": True,
                           "head": {"sha": HEAD}},
            ),
            mission=mission(),
        )
        self.assertEqual("MARK_READY", row["v1"]["output"])
        self.assertEqual("pager_equivalent", row["comparison_class"])
        self.assertEqual(
            "v4_pages_review_role_for_provider_effect",
            row["comparison_reason"],
        )

    def test_safety_waits_cannot_be_crossed(self):
        runner_wait = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(runner=True),
            mission=mission(),
        )
        self.assertEqual("WAIT", runner_wait["v1"]["output"])
        self.assertEqual("wait", runner_wait["v4"]["action"])
        self.assertEqual("match", runner_wait["comparison_class"])

        dependency_wait = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(deps=False),
            mission=mission(),
        )
        self.assertEqual("WAIT", dependency_wait["v1"]["output"])
        self.assertEqual("dependencies_unmet", dependency_wait["v4"]["reason"])
        self.assertEqual("match", dependency_wait["comparison_class"])

        stopped = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(scope="stopped"),
            mission=mission(),
        )
        self.assertEqual("WAIT", stopped["v1"]["output"])
        self.assertEqual("start_task", stopped["v4"]["action"])
        self.assertEqual("divergence", stopped["comparison_class"])
        self.assertEqual(
            "v4_pages_across_v1_safety_wait", stopped["comparison_reason"],
        )

    def test_role_mismatch_and_missing_event_block_release(self):
        mismatch = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(),
            mission=mission(role="remediation"),
        )
        self.assertEqual("divergence", mismatch["comparison_class"])
        self.assertEqual("requested_role_mismatch", mismatch["comparison_reason"])
        self.assertTrue(mismatch["release_blocked"])

        stuck = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(),
            mission=mission(handled=1, latest=1),
        )
        self.assertEqual("block_release", stuck["v4"]["action"])
        self.assertEqual("blocked", stuck["comparison_class"])
        self.assertEqual("missing_mission_event", stuck["comparison_reason"])
        self.assertTrue(stuck["release_blocked"])

    def test_human_and_terminal_must_be_present_in_v4(self):
        blocker = {
            "schema": "switchboard.work_session_human_blocker.v1",
            "source_tool": "agent_requires_human",
            "binding": "registered_agent",
            "agent_id": "codex/qa118",
            "provenance_stamp": "switchboard.resolve_write_actor.v1",
        }
        human_snapshot = snapshot(
            work_session={"status": "blocked", "hygiene": {"blocker": blocker}},
        )
        human = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=human_snapshot,
            mission=mission(state="HUMAN", handled=1, latest=1),
        )
        self.assertEqual("AGENT_REQUIRES_HUMAN", human["v1"]["output"])
        self.assertEqual("match", human["comparison_class"])

        merged_snapshot = snapshot(
            merge_provenance={"merged_sha": "b" * 40},
        )
        missing_terminal = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=merged_snapshot,
            mission=mission(),
        )
        self.assertEqual("OBSERVE_MERGED", missing_terminal["v1"]["output"])
        self.assertEqual("terminal_projection_missing",
                         missing_terminal["comparison_reason"])
        self.assertTrue(missing_terminal["release_blocked"])

        terminal = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=merged_snapshot,
            mission=mission(
                state="DONE", handled=1, latest=1,
                terminal_kind="github_merge", terminal_ref="b" * 40,
            ),
        )
        self.assertEqual("match", terminal["comparison_class"])

    def test_missing_mission_and_broken_reads_fail_closed(self):
        missing = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(),
            mission=None,
        )
        self.assertEqual("v4_mission_missing", missing["comparison_reason"])
        self.assertTrue(missing["release_blocked"])

        missing_liveness_source = compare_shadow_decisions(
            project=PROJECT,
            task_id="QA-118",
            snapshot=snapshot(source_observed_at={"task": 100.0}),
            mission=mission(),
        )
        self.assertEqual(
            "runner_liveness_source_missing",
            missing_liveness_source["comparison_reason"],
        )
        self.assertTrue(missing_liveness_source["release_blocked"])

        with self.assertRaisesRegex(RuntimeError, "hydration unavailable"):
            run_shadow_comparison(
                "QA-118",
                project=PROJECT,
                actor="test",
                journal=Journal({"QA-118": mission()}),
                hydrator=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("hydration unavailable")
                ),
                recorder=lambda *_args, **_kwargs: {"recorded": True},
            )

    def test_batch_records_every_proposal_and_blocks_empty_or_red_runs(self):
        rows = {"QA-118": mission(), "QA-119": mission(handled=1, latest=1)}
        recorded = []

        def hydrate(task_id, **_kwargs):
            return snapshot(task_id=task_id)

        def record(row, **_kwargs):
            recorded.append(row["task_id"])
            return {"recorded": True, "lifecycle_mutation": False}

        batch = run_shadow_batch(
            ["QA-118", "QA-119"],
            project=PROJECT,
            actor="test",
            journal=Journal(rows),
            hydrator=hydrate,
            recorder=record,
        )
        self.assertEqual(["QA-118", "QA-119"], recorded)
        self.assertEqual(2, batch["observation_count"])
        self.assertEqual(1, batch["blocker_count"])
        self.assertTrue(batch["release_blocked"])
        self.assertFalse(batch["cutover_authorized"])

        empty = run_shadow_batch(
            [], project=PROJECT, actor="test", journal=Journal({}),
            hydrator=hydrate, recorder=record,
        )
        self.assertFalse(empty["passed"])
        self.assertTrue(empty["release_blocked"])

        with self.assertRaisesRegex(RuntimeError, "shadow audit unavailable"):
            run_shadow_comparison(
                "QA-118", project=PROJECT, actor="test",
                journal=Journal(rows), hydrator=hydrate,
                recorder=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("shadow audit unavailable")
                ),
            )


if __name__ == "__main__":
    unittest.main()
