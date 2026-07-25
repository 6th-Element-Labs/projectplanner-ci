#!/usr/bin/env python3
"""COORD-52: attempt 7 must not look like attempt 1.

Implements §7 of docs/DECISION-CORPUS-OUTCOMES-SPEC.md. The live CO-20 assignment carried
no history, so seven consecutive remediations each believed they were the first. These
tests pin the bounded prior_attempts summary, its omission on a first dispatch, its caps,
and — most importantly — that adding it cannot break the exact-equality contract check that
every claim depends on.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.connect.execution_assignment import (
    ExecutionAssignmentError,
    build_execution_assignment,
    require_exact_execution_assignment,
)
from switchboard.domain.decisions.prior_attempts import (
    MAX_EXECUTION_IDS,
    MAX_OUTCOMES,
    MAX_ROUTES,
    SCHEMA,
    build_prior_attempts,
)

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def episode(reason_code, *, head="d150fd1b", route="remediation", outcome="superseded",
            execution_id="", generations_spent=0, head_advanced=False, features=None):
    return {
        "reason_code": reason_code, "head_sha": head, "route": route,
        "outcome": outcome, "execution_id": execution_id,
        "generations_spent": generations_spent, "head_advanced": head_advanced,
        "features": features or {},
    }


# --- first-ever dispatch carries nothing at all -----------------------------
ok(build_prior_attempts([], reason_code="required_exact_head_ci_failed") is None,
   "no episodes yields None, not a zeroed object")
ok(build_prior_attempts([episode("review_required")],
                        reason_code="required_exact_head_ci_failed") is None,
   "episodes for a different reason code do not count as prior attempts")
ok(build_prior_attempts([episode("x")], reason_code="") is None,
   "an empty reason code yields None")

# --- the CO-20 replay: seventh remediation on an unmoved head ---------------
CO20 = [episode("required_exact_head_ci_failed", execution_id=f"run_{i}")
        for i in range(6)]
summary = build_prior_attempts(
    CO20, reason_code="required_exact_head_ci_failed", head_sha="d150fd1b")
ok(summary is not None and summary["schema"] == SCHEMA, "summary carries its schema")
ok(summary["attempt_number"] == 7, "CO-20 replay reports attempt_number 7")
ok(summary["same_reason_code_on_this_head"] == 6,
   "CO-20 replay reports six prior attempts on this exact head")
ok(summary["head_advanced_since_first"] is False,
   "CO-20 replay reports the head never moved — the signal that says stop repeating")
ok(summary["routes_tried"] == ["remediation"], "distinct routes tried are listed once")
ok(all(o == "superseded" for o in summary["outcomes"]) and summary["outcomes"],
   "outcomes vector is populated with real (non-open) outcomes")

# --- head movement is detected both ways ------------------------------------
moved = build_prior_attempts(
    [episode("review_required", head="aaaa1111")],
    reason_code="review_required", head_sha="bbbb2222")
ok(moved["head_advanced_since_first"] is True,
   "a different head than the first attempt counts as advanced")
flagged = build_prior_attempts(
    [episode("review_required", head_advanced=True)],
    reason_code="review_required", head_sha="d150fd1b")
ok(flagged["head_advanced_since_first"] is True,
   "an episode that recorded head_advanced counts as advanced")

# --- bounded by construction: 200 episodes, fixed-size payload --------------
many = [episode("required_exact_head_ci_failed", execution_id=f"run_{i}",
                route=f"route_{i}", generations_spent=1) for i in range(200)]
big = build_prior_attempts(
    many, reason_code="required_exact_head_ci_failed", head_sha="d150fd1b")
ok(len(big["outcomes"]) == MAX_OUTCOMES, f"outcomes vector capped at {MAX_OUTCOMES}")
ok(len(big["routes_tried"]) == MAX_ROUTES, f"routes capped at {MAX_ROUTES}")
ok(len(big["recent_execution_ids"]) == MAX_EXECUTION_IDS,
   f"execution ids capped at {MAX_EXECUTION_IDS}")
ok(big["attempt_number"] == 201 and big["total_prior_episodes"] == 200,
   "counts report full magnitude even though vectors are truncated")
ok(big["truncated"] is True, "truncation is declared, never silent")
ok(big["generations_spent"] == 200, "generations_spent totals across all prior episodes")
ok(big["recent_execution_ids"][0] == "run_199", "most recent execution id comes first")

# --- COORD-61 near-miss pointer: WHERE the evidence already lives ------------
with_near_miss = build_prior_attempts(
    [episode("missing_executed_test_run", features={
        "missing_artifact": {
            "expected_key": "executed_test_run",
            "found_near_miss": [{"key": "executed_tests", "surface": "hygiene",
                                 "work_session_id": "worksession-aa0ccd80bb504bbd"}],
        }})],
    reason_code="missing_executed_test_run", head_sha="41092162")
ok(with_near_miss.get("evidence_session_id") == "worksession-aa0ccd80bb504bbd",
   "CO-21 replay: the repair learns which session holds the near-miss evidence")
ok("evidence_session_id" not in summary,
   "no near-miss recorded means the key is absent, not empty")

# --- THE CONTRACT SAFETY PROPERTY -------------------------------------------
# claims.py re-runs build_execution_assignment and compares with exact dict equality.
# A block derived from the live corpus would drift between dispatch and claim and fail
# EVERY claim. These pin the echo contract.
LIFECYCLE = {"role": "remediation", "head_sha": "d" * 40, "execution_id": "execlease-1",
             "generation": 2, "reason_code": "required_exact_head_ci_failed",
             "route": "remediation"}
ASSIGNMENT = {"assignment_id": "assignment-1"}

base = build_execution_assignment(
    task_id="CO-20", assignment=ASSIGNMENT, lifecycle=LIFECYCLE)
ok("prior_attempts" not in base,
   "a dispatch with no memory omits the key entirely from the contract")

dispatched = build_execution_assignment(
    task_id="CO-20", assignment=ASSIGNMENT, lifecycle=LIFECYCLE,
    prior_attempts=summary)
ok(dispatched["prior_attempts"]["attempt_number"] == 7,
   "the dispatched contract carries the memory block")

echoed = build_execution_assignment(
    task_id="CO-20", assignment=ASSIGNMENT, lifecycle=LIFECYCLE,
    prior_attempts=dispatched.get("prior_attempts"))
try:
    require_exact_execution_assignment(dispatched, echoed)
    ok(True, "echoing the stored block re-derives an identical contract (claim succeeds)")
except ExecutionAssignmentError:
    ok(False, "echoing the stored block must re-derive an identical contract")

drifted = build_execution_assignment(
    task_id="CO-20", assignment=ASSIGNMENT, lifecycle=LIFECYCLE,
    prior_attempts=build_prior_attempts(
        CO20 + [episode("required_exact_head_ci_failed")],
        reason_code="required_exact_head_ci_failed", head_sha="d150fd1b"))
try:
    require_exact_execution_assignment(dispatched, drifted)
    ok(False, "a re-derived (drifted) block must be rejected — proves why we echo")
except ExecutionAssignmentError as exc:
    ok(exc.code == "execution_assignment_contract_mismatch",
       "a corpus that moved between dispatch and claim would fail equality — hence echo")

# --- the wiring is pinned so it cannot silently regress ----------------------
coordination_src = (
    ROOT / "src/switchboard/storage/repositories/coordination.py").read_text()
claims_src = (ROOT / "src/switchboard/storage/repositories/claims.py").read_text()
ok("_dispatch_prior_attempts" in coordination_src
   and "prior_attempts=_dispatch_prior_attempts(" in coordination_src,
   "dispatch derives the memory block once")
ok('prior_attempts=contract.get("prior_attempts")' in claims_src,
   "the claim path echoes the stored block rather than re-deriving it")
ok("except Exception:" in coordination_src.split("_dispatch_prior_attempts")[2],
   "a corpus read failure cannot prevent a dispatch")

print(f"\nCOORD-52 prior attempts: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
