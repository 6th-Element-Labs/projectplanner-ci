#!/usr/bin/env python3
"""BUG-249: the immutable execution contract carries the task's evidence policy.

A managed execution minted with the strict code contract could launch an
``offline_evidence`` task but never truthfully complete it. The assignment now
stamps the resolved profile into the contract, and claim binding derives its
expected shape from that stamp. ``code_strict`` behavior and pre-profile
contracts stay byte-identical.
"""
from __future__ import annotations

import json
import sqlite3
import time
import unittest

from path_setup import ROOT as _ROOT  # noqa: F401
from switchboard.connect.execution_assignment import (
    ExecutionAssignmentError,
    build_execution_assignment,
    claim_expectations_for,
    require_exact_execution_assignment,
)
from switchboard.storage.repositories.claims import _stage_managed_completion_stop_in

TASK = "QA-33"
RUNNER = "run-bug249"
WAKE = "wake-bug249"


def _lifecycle(profile: str = "") -> dict:
    lifecycle = {
        "schema": "switchboard.execution_lifecycle.v1",
        "role": "implementation",
        "execution_id": "execlease-bug249",
        "generation": 2,
        "head_sha": "",
        "pr_number": 0,
        "pr_url": "",
        "reason_code": "needs_implementation",
    }
    if profile:
        lifecycle["session_policy_profile"] = profile
    return lifecycle


def _contract(profile: str = "") -> dict:
    return build_execution_assignment(
        task_id=TASK,
        assignment={"assignment_id": "assignment-bug249"},
        lifecycle=_lifecycle(profile),
    )


class ClaimExpectationShapeTest(unittest.TestCase):
    def test_offline_profile_relaxes_only_the_work_session(self):
        expectations = claim_expectations_for("offline_evidence", "implementation")
        self.assertEqual(
            {"required": True, "work_session_required": False,
             "role": "implementation"},
            expectations,
        )

    def test_absent_and_code_profiles_keep_the_strict_contract(self):
        for profile in ("", "code_strict", "docs_review", "unknown-thing"):
            self.assertEqual(
                {"required": True, "work_session_required": True,
                 "role": "implementation"},
                claim_expectations_for(profile, "implementation"),
            )

    def test_legacy_lifecycle_builds_a_byte_identical_contract(self):
        contract = _contract()
        self.assertNotIn("session_policy_profile", contract)
        self.assertEqual(
            {"required": True, "work_session_required": True,
             "role": "implementation"},
            contract["claim_expectations"],
        )

    def test_offline_lifecycle_stamps_the_profile(self):
        contract = _contract("offline_evidence")
        self.assertEqual("offline_evidence", contract["session_policy_profile"])
        self.assertFalse(contract["claim_expectations"]["work_session_required"])

    def test_rebuild_is_deterministic_and_tampering_is_rejected(self):
        stored = _contract("offline_evidence")
        require_exact_execution_assignment(stored, _contract("offline_evidence"))
        relaxed_without_stamp = _contract()
        relaxed_without_stamp["claim_expectations"]["work_session_required"] = False
        with self.assertRaises(ExecutionAssignmentError):
            require_exact_execution_assignment(relaxed_without_stamp, _contract())
        stripped = dict(stored)
        stripped.pop("session_policy_profile")
        with self.assertRaises(ExecutionAssignmentError):
            require_exact_execution_assignment(stripped, _contract("offline_evidence"))

    def test_bind_shape_check_follows_the_stamp(self):
        offline = _contract("offline_evidence")
        self.assertEqual(
            offline["claim_expectations"],
            claim_expectations_for(
                str(offline.get("session_policy_profile") or ""), "implementation"),
        )
        forged = _contract()
        forged["claim_expectations"]["work_session_required"] = False
        self.assertNotEqual(
            forged["claim_expectations"],
            claim_expectations_for(
                str(forged.get("session_policy_profile") or ""), "implementation"),
        )


def _completion_db(*, stamped_profile: str = "", with_wake: bool = True,
                   wake_id: str = WAKE) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE task_claims (
            id TEXT, task_id TEXT, runner_session_id TEXT,
            execution_generation INTEGER, execution_role TEXT, lease_epoch INTEGER
        );
        CREATE TABLE runner_sessions (
            runner_session_id TEXT, status TEXT, metadata_json TEXT,
            host_id TEXT, principal_id TEXT, heartbeat_ttl_s INTEGER,
            heartbeat_at REAL, updated_at REAL
        );
        CREATE TABLE resource_leases (
            id TEXT, resource_type TEXT, released_at REAL, task_id TEXT,
            execution_role TEXT, execution_generation INTEGER,
            fence_epoch INTEGER, ttl_seconds INTEGER, lease_state TEXT,
            claimed_at REAL
        );
        CREATE TABLE wake_intents (wake_id TEXT, policy_json TEXT);
        CREATE TABLE work_sessions (
            work_session_id TEXT, lease_epoch INTEGER, updated_at REAL
        );
        CREATE TABLE direct_session_tokens (
            runner_session_id TEXT, revoked_at REAL
        );
        CREATE TABLE task_execution_completion_phases (
            transition_id TEXT PRIMARY KEY, task_id TEXT, pr_number INTEGER,
            head_sha TEXT, runner_generation INTEGER, phase TEXT, outcome TEXT,
            evidence_json TEXT, failure_json TEXT, actor TEXT, transitioned_at REAL
        );
        CREATE TABLE activity (
            task_id TEXT, actor TEXT, kind TEXT, payload TEXT, created_at REAL
        );
        """
    )
    metadata = {
        "execution_id": "execlease-bug249",
        "execution_generation": 2,
        "execution_role": "implementation",
        "lease_epoch": 5,
        "wake_id": wake_id,
    }
    c.execute("INSERT INTO task_claims VALUES (?,?,?,?,?,?)",
              ("claim-bug249", TASK, RUNNER, 2, "implementation", 5))
    c.execute("INSERT INTO runner_sessions VALUES (?,?,?,?,?,?,?,?)",
              (RUNNER, "running", json.dumps(metadata), "host/test",
               "host-principal", 60, time.time(), time.time()))
    c.execute("INSERT INTO resource_leases VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("execlease-bug249", "execution", None, TASK, "implementation",
               2, 5, 900, "active", time.time()))
    if with_wake:
        contract = _contract(stamped_profile)
        c.execute("INSERT INTO wake_intents VALUES (?,?)",
                  (wake_id, json.dumps({"execution_assignment": contract})))
    return c


def _stage(c: sqlite3.Connection, evidence: dict) -> dict:
    claim = c.execute("SELECT * FROM task_claims").fetchone()
    return _stage_managed_completion_stop_in(
        c, claim, {}, evidence, "", "bug249-test", time.time())


class ManagedCompletionEvidenceTest(unittest.TestCase):
    """BUG-251 rule: honest evidence completes; no policy stamp is consulted.

    PR identity OR offline evidence enters the managed stopping/In Review
    handoff. Done authority (canonical merge provenance or the privileged
    offline verifier) is unchanged and remains the real fence.
    """

    def test_offline_evidence_completes_without_any_stamp(self):
        for db in (
            _completion_db(),                        # no profile stamped
            _completion_db(stamped_profile="docs_review"),
            _completion_db(stamped_profile="offline_evidence"),
            _completion_db(with_wake=False),         # wake row gone entirely
        ):
            result = _stage(db, {"offline_evidence": "drain canary transcript",
                                 "verification_note": "no PR by design"})
            self.assertTrue(result["stopping"])
            self.assertFalse(result["completed"])

    def test_empty_handed_completion_is_still_rejected(self):
        result = _stage(_completion_db(), {"note": "no evidence at all"})
        self.assertEqual("completion_identity_incomplete", result["reason"])
        self.assertEqual("missing_data", result["failure_class"])

    def test_pr_identity_still_completes(self):
        result = _stage(_completion_db(), {
            "pr_number": 77, "head_sha": "a" * 40})
        self.assertTrue(result["stopping"])

    def test_replayed_offline_completion_is_idempotent(self):
        c = _completion_db()
        evidence = {"offline_evidence": "drain canary transcript"}
        first = _stage(c, evidence)
        self.assertTrue(first["stopping"])
        replay = _stage(c, evidence)
        self.assertTrue(replay.get("idempotent"))
        phases = c.execute(
            "SELECT COUNT(*) FROM task_execution_completion_phases").fetchone()[0]
        self.assertEqual(1, phases)


if __name__ == "__main__":
    unittest.main()
