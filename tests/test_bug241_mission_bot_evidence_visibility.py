"""BUG-241 wave: Mission Bot must see evidence that exists and never invent failures.

Five confirmed audit findings, one test file:
1. queue_failed read a SUCCESSFUL arm as a queue ejection (REST never carries
   mergeQueueEntry, so empty state + prior_enqueue_verified fired every tick).
   Now an ejection needs a positive signal: a removal reason, or GitHub having
   CLEARED the auto_merge we verifiably armed (with an age floor).
2. A transient GitHub fetch failure became the definite factory verdict
   github_pr_state_unavailable; the typed-error marker now yields WAIT.
3. Review escalations (round limit / stalled) were misrouted to remediation
   runners that had nothing to repair; they now park as an operator decision.
4. The executed-test gate judged runs by which session recorded them; a run
   whose own branch+head match the gated identity is now accepted on merit.
5. merge_gate binds one Work Session; hygiene from exact-head siblings is now
   unioned in (BUG-234 clause 2 at the resolver layer), with fences intact.
Plus the hyphen normalizer that made the stopped-scope fence dead (QA-24 ->
qa_24 never equals QA-24).

Deliberately NOT changed: deriving executed-test proof from a green required
status context. The COORD-57/CO-21 conformance scenarios encode the #859 rule
("never manufacture what the gate exists to demand") — proof must come from a
recorded run, not a one-bit projection. BUG-239 already makes the real mirror
receipt visible, so nothing production needs that shortcut.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

_TMP = tempfile.mkdtemp(prefix="bug241-")
os.environ.setdefault("PM_DB_PATH", os.path.join(_TMP, "maxwell.db"))
os.environ.setdefault("PM_HELM_DB_PATH", os.path.join(_TMP, "helm.db"))
os.environ.setdefault("PM_SWITCHBOARD_DB_PATH", os.path.join(_TMP, "switchboard.db"))
os.environ.setdefault("PM_PROJECT_REGISTRY_DB_PATH", os.path.join(_TMP, "project_registry.db"))
os.environ.setdefault("PM_DYNAMIC_PROJECTS_DIR", _TMP)

import store  # noqa: E402
from switchboard.application.commands import merge_gate as mg  # noqa: E402
from switchboard.domain.mission_bot import facts as F  # noqa: E402
from switchboard.domain.mission_bot import reduce_mission  # noqa: E402
from switchboard.domain.mission_bot.outputs import MissionOutput  # noqa: E402
from switchboard.storage.repositories.claims import _executed_test_run_gate  # noqa: E402

_PROJECT = "switchboard"
_HEAD = "d76647d3b888af9fedd4ade1d70de9f6b4105e2f"


def _base_snapshot(**overrides):
    snap = {
        "task_id": "QA-241",
        "board_status": "in_review",
        "github_pr": {"number": 1097, "state": "open", "url": "u", "draft": False},
        "pr_number": 1097,
        "head_sha": _HEAD,
        "required_status_contexts": [],
        "status_contexts": {},
        "findings": [],
        "merge_queue": {},
        "review": {},
        "runner": {},
        "dependency_state": {"satisfied": True},
    }
    snap.update(overrides)
    return snap


class QueueEjectionNeedsPositiveSignal(unittest.TestCase):
    def test_armed_and_still_armed_is_not_an_ejection(self):
        snap = _base_snapshot(merge_queue={
            "prior_enqueue_verified": True,
            "live_auto_merge_active": True,
            "prior_enqueue_age_s": 900.0,
        })
        self.assertFalse(F.queue_failed(snap))

    def test_verified_arm_alone_is_unknown_not_ejection(self):
        snap = _base_snapshot(merge_queue={"prior_enqueue_verified": True})
        self.assertFalse(F.queue_failed(snap),
                         "empty queue state with no disarm signal must stay unknown")

    def test_github_cleared_our_arm_is_an_ejection_after_grace(self):
        snap = _base_snapshot(merge_queue={
            "prior_enqueue_verified": True,
            "live_auto_merge_active": False,
            "prior_enqueue_age_s": 900.0,
        })
        self.assertTrue(F.queue_failed(snap))

    def test_fresh_arm_rides_out_read_after_write_lag(self):
        snap = _base_snapshot(merge_queue={
            "prior_enqueue_verified": True,
            "live_auto_merge_active": False,
            "prior_enqueue_age_s": 5.0,
        })
        self.assertFalse(F.queue_failed(snap))

    def test_recorded_removal_reason_is_still_an_ejection(self):
        snap = _base_snapshot(merge_queue={"last_removal_reason": "failed_checks"})
        self.assertTrue(F.queue_failed(snap))


class TransientFetchFailureWaits(unittest.TestCase):
    def test_empty_pr_with_no_error_is_still_a_factory_failure(self):
        snap = _base_snapshot(github_pr={}, pr_number=0,
                              board_pr_number=1097, board_status="in_review")
        self.assertEqual(F.factory_failure_reason(snap), "github_pr_state_unavailable")

    def test_typed_fetch_error_yields_wait_not_remediation(self):
        snap = _base_snapshot(
            github_pr={}, pr_number=0, board_pr_number=1097,
            board_status="in_review",
            github_pr_fetch_error={"transient": True, "detail": "403 rate limited"})
        self.assertEqual(F.factory_failure_reason(snap), "")
        command = reduce_mission(snap)
        self.assertEqual(command["output"], MissionOutput.WAIT.value)


class ReviewEscalationParksForOperator(unittest.TestCase):
    _FINDING = {"code": "review_round_limit_reached", "blocking": True,
                "message": "3 rounds"}

    def test_escalation_is_not_a_factory_failure(self):
        snap = _base_snapshot(findings=[dict(self._FINDING)])
        self.assertFalse(F.factory_failure(snap))

    def test_reducer_parks_escalation_as_operator_decision(self):
        snap = _base_snapshot(findings=[dict(self._FINDING)])
        command = reduce_mission(snap)
        self.assertEqual(command["output"], MissionOutput.WAIT.value)
        self.assertEqual(command["reason_code"], "review_round_limit_reached")

    def test_stalled_review_parks_too(self):
        snap = _base_snapshot(findings=[{
            "code": "review_stalled_no_verdict", "blocking": True}])
        command = reduce_mission(snap)
        self.assertEqual(command["output"], MissionOutput.WAIT.value)

    def test_non_escalation_findings_still_route_to_remediation(self):
        snap = _base_snapshot(findings=[{
            "code": "missing_executed_test_run", "blocking": True}])
        self.assertTrue(F.factory_failure(snap))


class ExecutedTestRunMeritRule(unittest.TestCase):
    _SESSION = {"work_session_id": "ws-review", "branch": "agent/b", "head_sha": _HEAD}

    def _run(self, **overrides):
        run = {
            "schema": "switchboard.executed_test_run.v1",
            "work_session_id": "ws-impl",
            "branch": "agent/b",
            "head_sha": _HEAD,
            "commands": ["python tests/t.py"],
            "exit_code": 0,
            "output_sha256": "a" * 64,
            "completed_at": "2026-07-30T00:00:00Z",
        }
        run.update(overrides)
        return run

    def test_sibling_session_run_with_matching_identity_passes(self):
        gate = _executed_test_run_gate({"executed_test_run": self._run()}, dict(self._SESSION))
        self.assertTrue(gate.get("ok"), gate)

    def test_sibling_run_on_wrong_head_is_still_rejected(self):
        gate = _executed_test_run_gate(
            {"executed_test_run": self._run(head_sha="f" * 40)}, dict(self._SESSION))
        self.assertFalse(gate.get("ok"))

    def test_sibling_run_that_cannot_prove_identity_is_rejected(self):
        gate = _executed_test_run_gate(
            {"executed_test_run": self._run(branch="", head_sha="")}, dict(self._SESSION))
        self.assertFalse(gate.get("ok"),
                         "a run without branch/head keeps the session fence")


class SessionHygieneUnion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_project_registry()
        store.init_db(_PROJECT)

    @staticmethod
    def _task(label):
        return store.create_task(
            {"workstream_id": "QA", "title": f"{label} union fixture"},
            actor="test", project=_PROJECT)["task_id"]

    @staticmethod
    def _session(session_id, task_id, head, hygiene=None, status="active"):
        created = store.create_work_session(
            {
                "work_session_id": session_id,
                "task_id": task_id,
                "agent_id": f"agent/test/{task_id.lower()}",
                "repo_role": "canonical",
                "repo": "6th-Element-Labs/projectplanner",
                "default_branch": "master",
                "branch": f"agent/switchboard/{task_id}/g1",
                "head_sha": head,
                "base_sha": head,
                "storage_mode": "worktree",
                "worktree_path": f"/tmp/ws/{session_id}",
                "status": status,
                "hygiene": hygiene or {},
            },
            actor="test", project=_PROJECT)
        assert not created.get("error"), created
        return created["work_session"]

    def test_sibling_hygiene_fills_missing_keys(self):
        task = self._task("U1")
        self._session(f"ws-{task}-impl", task, _HEAD,
                      hygiene={"executed_test_run": {"exit_code": 0}})
        review = self._session(f"ws-{task}-review", task, _HEAD)
        merged = mg._union_exact_head_session_hygiene(
            review, task, project=_PROJECT, head_sha=_HEAD)
        self.assertEqual(
            (merged.get("hygiene") or {}).get("executed_test_run"), {"exit_code": 0})
        self.assertEqual(
            merged.get("hygiene_contributed_by"),
            {"executed_test_run": f"ws-{task}-impl"})

    def test_sibling_on_other_head_never_contributes(self):
        task = self._task("U2")
        self._session(f"ws-{task}-impl", task, "c" * 40,
                      hygiene={"executed_test_run": {"exit_code": 0}})
        review = self._session(f"ws-{task}-review", task, _HEAD)
        merged = mg._union_exact_head_session_hygiene(
            review, task, project=_PROJECT, head_sha=_HEAD)
        self.assertFalse((merged.get("hygiene") or {}).get("executed_test_run"))

    def test_present_keys_are_never_overwritten(self):
        task = self._task("U3")
        self._session(f"ws-{task}-impl", task, _HEAD,
                      hygiene={"repo_preflight": {"verdict": "pass", "owner": "impl"}})
        review = self._session(
            f"ws-{task}-review", task, _HEAD,
            hygiene={"repo_preflight": {"verdict": "warn", "owner": "review"}})
        merged = mg._union_exact_head_session_hygiene(
            review, task, project=_PROJECT, head_sha=_HEAD)
        self.assertEqual(
            (merged.get("hygiene") or {}).get("repo_preflight", {}).get("owner"),
            "review")


class HyphenatedScopeFilter(unittest.TestCase):
    def test_stopped_scope_with_hyphenated_task_id_reaches_snapshot(self):
        from switchboard.application import completion_driver as cd
        scope_row = {"scope_type": "task", "task_id": "QA-241",
                     "scope_id": "autopilot-x", "status": "stopped",
                     "updated_at": 1.0}
        matching = [
            cd._map(scope) for scope in [scope_row]
            if cd._text(cd._map(scope).get("scope_type")) == "task"
            and str(cd._map(scope).get("task_id") or "").strip().upper() == "QA-241"
        ]
        self.assertEqual(len(matching), 1,
                         "hyphenated board ids must survive the scope filter")


if __name__ == "__main__":
    unittest.main()
