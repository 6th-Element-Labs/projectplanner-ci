#!/usr/bin/env python3
"""COORD-46: one idempotent effect per persisted route.

The owner executes exactly one effect, then rehydrates and classifies again.
Planning that effect is pure, so the interesting rules -- which effect a route
earns, when a live generation must be fenced, and what makes two ticks the same
effect -- are provable without touching GitHub, the board, or a runner.
"""
from __future__ import annotations

import unittest

from path_setup import ROOT  # noqa: F401

from switchboard.domain.completion import effects  # noqa: E402
from switchboard.domain.completion.state_machine import (  # noqa: E402
    build_completion_snapshot, classify_completion,
)

HEAD = "a" * 40
OLD = "b" * 40


def _snap(**kw):
    base = {"head_sha": HEAD, "pr_number": 810, "task_id": "COORD-41"}
    base.update(kw)
    return base


def _run(**kw):
    base = {"run_id": "run-1", "state_version": 3, "attempt": 0}
    base.update(kw)
    return base


def _plan(route, *, effect="none", role=None, snapshot=None, run=None,
          runner=None, state="blocked"):
    decision = {"state": state, "route": route, "reason_code": "r",
                "desired_role": role, "effect": effect}
    snap = dict(snapshot or _snap())
    if runner is not None:
        snap["runner"] = runner
    return effects.plan_effect(decision, snap, run or _run())


class RouteToEffect(unittest.TestCase):
    def test_wait_performs_no_side_effect(self):
        plan = _plan("wait", state="waiting")
        self.assertEqual(plan["effect"], "wait")
        self.assertFalse(plan["mutates"])

    def test_none_is_terminal_and_does_nothing(self):
        plan = _plan("none", state="done")
        self.assertEqual(plan["effect"], "none")
        self.assertFalse(plan["mutates"])

    def test_review_merge_ensures_an_exact_head_generation(self):
        plan = _plan("review_merge", role="review_merge")
        self.assertEqual(plan["effect"], "ensure_review_generation")
        self.assertEqual(plan["role"], "review_merge")
        self.assertEqual(plan["head_sha"], HEAD)

    def test_draft_ready_plans_mark_ready_then_reread(self):
        plan = _plan("review_merge", effect="mark_ready_then_reread",
                     role="review_merge", state="ready_to_queue")
        self.assertEqual(plan["effect"], "mark_ready")
        self.assertTrue(plan["reread_after"])

    def test_clean_snapshot_plans_a_single_enqueue(self):
        plan = _plan("review_merge", effect="enqueue", role="review_merge",
                     state="ready_to_queue")
        self.assertEqual(plan["effect"], "enqueue")
        self.assertTrue(plan["once_only"])

    def test_remediation_queues_coord20_and_starts_a_generation(self):
        plan = _plan("remediation", role="remediation")
        self.assertEqual(plan["effect"], "start_remediation")
        self.assertEqual(plan["role"], "remediation")
        self.assertTrue(plan["queue_remediation_round"])

    def test_human_emits_exactly_one_escalation(self):
        plan = _plan("human")
        self.assertEqual(plan["effect"], "escalate_human")
        self.assertTrue(plan["once_only"])

    def test_reconcile_reads_canonical_provenance(self):
        plan = _plan("reconcile", state="reconciling")
        self.assertEqual(plan["effect"], "reconcile_provenance")

    def test_merge_group_infrastructure_failure_requeues(self):
        plan = _plan("coordination_retry", snapshot=_snap(
            merge_queue={"state": "unmergeable",
                         "failure_attribution": "infrastructure"}))
        self.assertEqual(plan["effect"], "requeue_merge_group")

    def test_generic_coordination_retry_repairs_dispatch(self):
        self.assertEqual(_plan("coordination_retry")["effect"], "repair_dispatch")


class ClassifierBeatsLiveRunner(unittest.TestCase):
    """A decision outranks whatever process happens to be running."""

    def test_live_review_merge_is_fenced_when_remediation_is_required(self):
        plan = _plan("remediation", role="remediation",
                     runner={"live": True, "role": "review_merge",
                             "head_sha": HEAD, "generation": 7})
        self.assertTrue(plan["fence_required"])
        self.assertEqual(plan["fence_generation"], 7)
        self.assertEqual(plan["effect"], "start_remediation")

    def test_stale_head_runner_is_fenced_even_with_the_right_role(self):
        plan = _plan("review_merge", role="review_merge",
                     runner={"live": True, "role": "review_merge",
                             "head_sha": OLD, "generation": 2})
        self.assertTrue(plan["fence_required"])

    def test_matching_role_and_head_attaches_instead_of_restarting(self):
        plan = _plan("review_merge", role="review_merge",
                     runner={"live": True, "role": "review_merge",
                             "head_sha": HEAD, "generation": 5})
        self.assertFalse(plan["fence_required"])
        self.assertEqual(plan["effect"], "attach_and_wait")
        self.assertFalse(plan["mutates"])

    def test_a_dead_runner_never_blocks_a_fresh_generation(self):
        plan = _plan("remediation", role="remediation",
                     runner={"live": False, "role": "review_merge",
                             "head_sha": OLD})
        self.assertFalse(plan["fence_required"])
        self.assertEqual(plan["effect"], "start_remediation")


class Idempotency(unittest.TestCase):
    """Acceptance 3: duplicate ticks cannot duplicate anything."""

    def test_same_decision_yields_the_same_key(self):
        self.assertEqual(_plan("remediation", role="remediation")["idem_key"],
                         _plan("remediation", role="remediation")["idem_key"])

    def test_a_new_head_yields_a_new_key(self):
        self.assertNotEqual(
            _plan("remediation", role="remediation")["idem_key"],
            _plan("remediation", role="remediation",
                  snapshot=_snap(head_sha=OLD))["idem_key"])

    def test_a_new_route_yields_a_new_key(self):
        self.assertNotEqual(_plan("remediation", role="remediation")["idem_key"],
                            _plan("review_merge", role="review_merge")["idem_key"])

    def test_a_new_attempt_yields_a_new_key(self):
        self.assertNotEqual(
            _plan("remediation", role="remediation")["idem_key"],
            _plan("remediation", role="remediation",
                  run=_run(attempt=1))["idem_key"])

    def test_a_new_state_version_yields_a_new_key(self):
        self.assertNotEqual(
            _plan("remediation", role="remediation")["idem_key"],
            _plan("remediation", role="remediation",
                  run=_run(state_version=4))["idem_key"])

    def test_liveness_values_never_enter_the_key(self):
        """Lease/heartbeat/expiry churn must not create a new effect."""
        quiet = _plan("remediation", role="remediation",
                      runner={"live": True, "role": "remediation",
                              "head_sha": HEAD, "generation": 1,
                              "heartbeat_at": 1, "expires_at": 2,
                              "lease_renewed_at": 3})
        noisy = _plan("remediation", role="remediation",
                      runner={"live": True, "role": "remediation",
                              "head_sha": HEAD, "generation": 1,
                              "heartbeat_at": 999, "expires_at": 1000,
                              "lease_renewed_at": 1001})
        self.assertEqual(quiet["idem_key"], noisy["idem_key"])


class EndToEndFixtures(unittest.TestCase):
    """Acceptance 1 and 2, driven through the real classifier."""

    def test_pr810_routes_remediation_and_fences_live_review(self):
        head = "88624a605727fd44df98191d5b7dd99c73b75d9c"
        snapshot = build_completion_snapshot(
            task={"task_id": "COORD-41", "status": "In Review",
                  "git_state": {"head_sha": head}},
            github_pr={"number": 810, "state": "open", "draft": True,
                       "mergeable": True, "mergeStateStatus": "BLOCKED",
                       "head": {"sha": head}},
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=[{"name": "Switchboard CI / VM gate",
                              "conclusion": "failure",
                              "failure_attribution": "product"}],
            review={"status": "passed", "head_sha": head},
            runner={"live": True, "role": "review_merge", "head_sha": head,
                    "generation": 9},
        )
        decision = classify_completion(None, snapshot)
        self.assertEqual(decision["route"], "remediation")
        self.assertEqual(decision["reason_code"], "required_exact_head_ci_failed")

        plan = effects.plan_effect(decision, snapshot, _run())
        self.assertEqual(plan["effect"], "start_remediation")
        self.assertTrue(plan["fence_required"])
        self.assertEqual(plan["fence_generation"], 9)
        self.assertEqual(plan["head_sha"], head)

    def test_pr811_routes_review_merge_at_current_head_without_a_coder(self):
        head = "ebd76cbf01603880d16e5ab84071da17885334b1"
        snapshot = build_completion_snapshot(
            task={"task_id": "ADAPTER-23", "status": "In Review",
                  "git_state": {"head_sha": head}},
            github_pr={"number": 811, "state": "open", "draft": True,
                       "mergeable": True, "mergeStateStatus": "CLEAN",
                       "head": {"sha": head}},
            required_status_contexts=["Switchboard CI / VM gate"],
            status_contexts=[{"name": "Switchboard CI / VM gate",
                              "conclusion": "success"}],
            review={"status": "passed", "head_sha": head},
            runner={"live": False},
        )
        decision = classify_completion(None, snapshot)
        plan = effects.plan_effect(decision, snapshot, _run())
        # Draft with every substantive gate green: mark ready, then re-read the
        # exact head before enqueueing. Never a remediation coder.
        self.assertEqual(decision["route"], "review_merge")
        self.assertEqual(plan["effect"], "mark_ready")
        self.assertTrue(plan["reread_after"])
        self.assertNotEqual(plan["role"], "remediation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
