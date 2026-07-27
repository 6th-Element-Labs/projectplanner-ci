#!/usr/bin/env python3
"""Build the decision-quality gold catalog for completion conformance.

    python3.11 scripts/completion_conformance/build_gold_catalog.py

This is the human/spec oracle. Every row below states, from
``docs/AUTOPILOT-COMPLETION-STATE-MACHINE.md``, the route a correct
implementation must choose for one scenario world -- *before* the production
classifier ever runs. The script then:

1. builds the same ``build_completion_snapshot`` world T1 uses;
2. drives the real ``run_completion_tick`` (hermetic storage patches, same as
   T1) twice, proving idempotent replay;
3. compares the actual route to the spec route;
4. on a match, locks the *rest* of the decision (terminal, reason_code,
   role_sequence, effect) from the observed tick and writes
   ``tests/conformance/scenarios/gold/<id>.json``;
5. on a mismatch, records a SPEC_MISMATCH -- a real gap between the doc and
   the code -- and never writes a gold file for it.

Grading is against the spec, never against the classifier's own output: a
row is gold only because its *route* was predicted by the doc first.

Re-runnable: every run wipes and regenerates the gold directory and
``tests/conformance/GOLD_COVERAGE.md`` from this catalog.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
CONFORMANCE = TESTS / "conformance"
for _entry in (str(ROOT), str(SRC)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from tests.conformance import _evidence  # noqa: E402
from tests.conformance import _gold_shared  # noqa: E402
from tests.conformance import _shared  # noqa: E402

GOLD_DIR = CONFORMANCE / "scenarios" / "gold"
COVERAGE_DOC = CONFORMANCE / "GOLD_COVERAGE.md"
SPEC_DOC = "docs/AUTOPILOT-COMPLETION-STATE-MACHINE.md"

# ---------------------------------------------------------------------------
# World builders -- the human/spec oracle.
# ---------------------------------------------------------------------------

#: Every axis clean and green. Every catalog row overrides only what it needs
#: to exercise, so "else clean" (per the task brief) is enforced by
#: construction rather than by hand-copying nine fields per scenario.
BASE_WORLD: dict[str, Any] = {
    "draft": False,
    "ci": "pass",
    "ci_context": "Switchboard CI / VM gate",
    "ci_attribution": "product",
    "review": "passed",
    "mergeable": True,
    "merge_state_status": "CLEAN",
    "queue": "none",
    "runner": {"live": False, "role": "none", "head": "none"},
}

TIMING: dict[str, Any] = {
    "ci_delay_seconds": 0,
    "never_report": False,
    "hang_seconds": 0,
    "close_pr_while_runner_live": False,
    "move_head_during_assessment": False,
}


def _world(**overrides: Any) -> dict[str, Any]:
    world = copy.deepcopy(BASE_WORLD)
    runner_override = overrides.pop("runner", None)
    evidence_override = overrides.pop("evidence", None)
    world.update(overrides)
    if runner_override is not None:
        world["runner"] = {**world["runner"], **runner_override}
    if evidence_override is not None:
        # COORD-78: an evidence row states only the axis it exercises, and the
        # rest of the evidence world is filled from the same derived default a
        # scenario that declares no evidence at all would get. Written out in
        # full so the gold file records the complete world the gate ran against
        # rather than a diff against a default that could later move.
        world["evidence"] = {
            **_evidence.default_evidence(world),
            **evidence_override,
        }
    return world


# ---------------------------------------------------------------------------
# Catalog: (id, world, spec_route, spec_source, note)
#
# spec_route is the ONLY oracle input. Everything else (terminal,
# reason_code, role_sequence, effect) is locked from the observed tick once
# the route matches -- per the mandatory method, we never invent gold by
# trusting the classifier without a spec route.
# ---------------------------------------------------------------------------


class Row:
    def __init__(
        self,
        scenario_id: str,
        world: dict[str, Any],
        spec_route: str,
        spec_source: str,
        note: str,
        category: str,
    ) -> None:
        self.id = scenario_id
        self.world = world
        self.spec_route = spec_route
        self.spec_source = spec_source
        self.note = note
        self.category = category


CATALOG: list[Row] = [
    # --- A. Clean CI slice -------------------------------------------------
    Row(
        "clean_ci_pass",
        _world(),
        "review_merge",
        "CI mapping: SUCCESS -> Pass; Required regression matrix: enqueue "
        "happens exactly once",
        "All gates green, nothing else dirty.",
        "clean",
    ),
    Row(
        "clean_ci_fail",
        _world(ci="fail", merge_state_status="BLOCKED"),
        "remediation",
        "CI mapping: FAILURE with product attribution -> remediation",
        "Product CI failure, else clean.",
        "clean",
    ),
    Row(
        "clean_ci_error",
        _world(ci="error", ci_attribution="infrastructure",
               merge_state_status="BLOCKED"),
        "coordination_retry",
        "CI mapping: ERROR with infrastructure attribution -> coordination_retry",
        "Infra CI failure, else clean -- product vs. infra route differently.",
        "clean",
    ),
    Row(
        "clean_ci_pending",
        _world(ci="pending"),
        "wait",
        "CI mapping: PENDING/IN_PROGRESS -> wait",
        "Pending CI waits without starting another generation.",
        "clean",
    ),
    Row(
        "clean_ci_missing",
        _world(ci="missing"),
        "coordination_retry",
        "CI mapping: missing beyond dispatch deadline -> coordination_retry",
        "No CI observation at all for a required context, else clean.",
        "clean",
    ),

    # --- B. Scar / regression rows ------------------------------------------
    Row(
        "draft_red_ci",
        _world(draft=True, ci="fail", merge_state_status="BLOCKED"),
        "remediation",
        "Draft mapping: draft=true with substantive failure routes from the "
        "failure; Invariant 7: draft never masks another failure",
        "Draft plus red CI must still remediate -- draft never masks red CI.",
        "scar",
    ),
    Row(
        "draft_green_mark_ready",
        _world(draft=True),
        "review_merge",
        "Draft mapping: draft=true, all substantive gates green -> "
        "review_merge, mark ready, re-read exact head",
        "Draft plus green CI and passed review routes to mark-ready.",
        "scar",
    ),
    Row(
        "draft_pending_mark_ready",
        _world(draft=True, ci="pending", merge_state_status="BLOCKED"),
        "review_merge",
        "Draft mapping: draft=true with pending CI -> review_merge mark ready "
        "(BREAKDOWN 5 / COORD-56); pending CI wait must not hide draft",
        "Draft plus pending CI marks ready immediately; red product CI still "
        "outranks draft (see draft_red_ci).",
        "scar",
    ),
    Row(
        "pending_ci_waits",
        _world(ci="pending"),
        "wait",
        "Required regression matrix: pending CI waits without starting "
        "another generation",
        "Alias of clean_ci_pending kept as its own id per the regression "
        "matrix bullet it proves. Non-draft only -- draft+pending is "
        "draft_pending_mark_ready.",
        "scar",
    ),
    Row(
        "missing_review",
        _world(review="missing"),
        "review_merge",
        "Review mapping: Missing or stale verdict -> review_merge",
        "CI green, review missing, else clean.",
        "scar",
    ),
    Row(
        "changes_requested_judgment_human",
        _world(review="changes_requested"),
        "human",
        "Review mapping: CHANGES_REQUESTED with judgment or authority "
        "findings -> human",
        "Empty review findings (review_findings omitted/none) take the "
        "judgment/human path. Contrast changes_requested_automatic_remediation.",
        "scar",
    ),
    Row(
        "merge_conflict",
        _world(mergeable=False, merge_state_status="DIRTY"),
        "remediation",
        "Mergeability mapping: CONFLICTING -> remediation",
        "mergeable=false, else clean -- an agent-fixable rebase, not "
        "coordination retry.",
        "scar",
    ),
    Row(
        "queue_awaiting",
        _world(queue="queued"),
        "wait",
        "Merge queue mapping: AWAITING_CHECKS -> wait",
        "Queue entry present, else as green as possible.",
        "scar",
    ),
    Row(
        "queue_unmergeable_product",
        _world(queue="unmergeable", ci="fail", ci_attribution="product"),
        "remediation",
        "Merge queue mapping: UNMERGEABLE, product check failure -> "
        "remediation",
        "Queue attributes the failure to product; classifier must decompose "
        "the queue finding, not the CI finding (queue is checked first).",
        "scar",
    ),
    Row(
        "queue_unmergeable_infra",
        _world(queue="unmergeable", ci="error", ci_attribution="infrastructure"),
        "coordination_retry",
        "Merge queue mapping: UNMERGEABLE, infrastructure failure -> "
        "coordination_retry and requeue",
        "Queue attributes the failure to infrastructure -> requeue, not a "
        "remediation coder.",
        "scar",
    ),
    Row(
        "tip_green_after_queue_eject",
        _world(queue="ejected", queue_removal_reason="failed_checks"),
        "coordination_retry",
        "Merge queue mapping: No entry after verified enqueue eject "
        "(failed_checks / checks_timed_out), tip still green -> "
        "coordination_retry and requeue_merge_group "
        "(merge_queue_ejected_tip_green)",
        "BREAKDOWN 38/40: tip CLEAN after GitHub ejected a prior verified "
        "enqueue must requeue, not replay once-only enqueue.",
        "scar",
    ),
    Row(
        "tip_green_after_checks_timed_out",
        _world(queue="ejected", queue_removal_reason="checks_timed_out"),
        "coordination_retry",
        "Merge queue mapping: No entry after verified enqueue eject "
        "(failed_checks / checks_timed_out), tip still green -> "
        "coordination_retry and requeue_merge_group "
        "(merge_queue_ejected_tip_green)",
        "Same eject→requeue contract with checks_timed_out removal reason.",
        "scar",
    ),
    Row(
        "pr_merged_reconcile",
        _world(pr_state="merged"),
        "reconcile",
        "Required regression matrix / terminal mapping: merged PR -> "
        "reconcile via canonical_pr_merged",
        "PR lifecycle MERGED with else-clean gates routes to provenance "
        "reconcile, not enqueue.",
        "scar",
    ),
    Row(
        "pr_closed_unmerged_human",
        _world(pr_state="closed"),
        "human",
        "Required regression matrix: CLOSED unmerged without reopen "
        "authorization -> human",
        "Closed, not merged, reopen not authorized.",
        "scar",
    ),
    Row(
        "pr_closed_unmerged_reopen",
        _world(pr_state="closed", reopen_authorized=True),
        "coordination_retry",
        "Required regression matrix: CLOSED unmerged with reopen "
        "authorization -> coordination_retry",
        "Authorized reopen is orchestration repair, not human escalation.",
        "scar",
    ),
    Row(
        "clean_ci_timed_out",
        _world(ci="timed_out", ci_attribution="infrastructure",
               merge_state_status="BLOCKED"),
        "coordination_retry",
        "CI mapping: TIMED_OUT with infrastructure attribution -> "
        "coordination_retry",
        "Distinct from ERROR: same route, different GitHub conclusion string.",
        "scar",
    ),
    Row(
        "clean_ci_action_required",
        _world(ci="action_required", merge_state_status="BLOCKED"),
        "human",
        "CI mapping: ACTION_REQUIRED -> human via "
        "required_ci_authority_failure",
        "Authority/policy CI conclusion is never a coder remediation.",
        "scar",
    ),
    Row(
        "clean_ci_cancelled",
        _world(ci="cancelled", ci_attribution="infrastructure",
               merge_state_status="BLOCKED"),
        "coordination_retry",
        "CI mapping / Required regression matrix: cancelled CI retries "
        "coordination",
        "cancelled/stale/startup_failure share coordination_retry; this "
        "row locks the cancelled reason code.",
        "scar",
    ),
    Row(
        "queue_locked_waits",
        _world(queue="locked"),
        "wait",
        "Merge queue mapping: LOCKED -> Bounded wait, then "
        "coordination_retry",
        "Locked without retry_exhausted stays on the wait path.",
        "scar",
    ),
    Row(
        "queue_locked_retry_exhausted",
        _world(queue="locked", queue_retry_exhausted=True),
        "coordination_retry",
        "Merge queue mapping: LOCKED with retry exhausted -> "
        "coordination_retry (merge_queue_locked)",
        "Exhausted locked dwell escalates coordination repair.",
        "scar",
    ),
    Row(
        "review_retry_budget_exhausted",
        _world(review="changes_requested", review_retry_exhausted=True),
        "human",
        "Review mapping: CHANGES_REQUESTED with retry budget exhausted -> "
        "human via review_retry_budget_exhausted",
        "Exhausted review rounds are never another automatic remediation.",
        "scar",
    ),
    Row(
        "changes_requested_automatic_remediation",
        _world(review="changes_requested", review_findings="automatic"),
        "remediation",
        "Review mapping: CHANGES_REQUESTED with automatic findings -> "
        "remediation; Required regression matrix: automatic requested "
        "changes start remediation",
        "Contrast to changes_requested_judgment_human: automatic findings "
        "dispatch a remediation coder.",
        "scar",
    ),
    Row(
        "live_wrong_role_fence",
        _world(runner={"live": True, "role": "review_merge", "head": "same"}),
        "coordination_retry",
        "Runner mapping: Running with wrong role -> fence exact generation, "
        "then start desired role; Invariant 5: only the generation matching "
        "desired role and exact head may be attached",
        "Snapshot is fully clean/green (desired role is none -- about to "
        "enqueue directly), yet a runner is still live. Any live runner in "
        "that state is, by definition, the wrong generation and must be "
        "fenced.",
        "scar",
    ),
    Row(
        "mergeability_behind",
        _world(merge_state_status="BEHIND"),
        "coordination_retry",
        "mergeStateStatus mapping: BEHIND -> coordination_retry to update "
        "the branch (merge queue owns it when available)",
        "Aggregate says BEHIND with everything else green; T1's fixture has "
        "no merge queue available here, so the branch-update repair path "
        "applies.",
        "scar",
    ),
    Row(
        "mergeability_unknown",
        _world(merge_state_status="UNKNOWN", mergeable=None),
        "wait",
        "Mergeability mapping / mergeStateStatus mapping: UNKNOWN -> bounded "
        "wait, then coordination_retry",
        "Unknown mergeability with no retry budget exhausted yet -> the "
        "bounded wait, not the escalation.",
        "scar",
    ),
    Row(
        "aggregate_blocked_not_masking_green",
        _world(merge_state_status="BLOCKED"),
        "review_merge",
        "mergeStateStatus mapping: BLOCKED -- decompose through CI, review, "
        "draft, policy, and conflict; never route directly from this "
        "aggregate; Invariant: only a clean, green, exact-head snapshot "
        "advances to READY_TO_QUEUE",
        "Proves the negative: an aggregate BLOCKED with every underlying "
        "gate actually green must NOT block. If this ever mismatches, the "
        "classifier started trusting the aggregate again (COORD-49 class "
        "regression).",
        "scar",
    ),
    Row(
        "remediation_fences_live_review_merge",
        _world(ci="fail",
               runner={"live": True, "role": "review_merge", "head": "same"}),
        "remediation",
        "Required regression matrix: a live review_merge is fenced when "
        "remediation becomes higher priority",
        "Desired role is remediation; live generation is review_merge at "
        "the same head -- role mismatch alone must fence and replace.",
        "scar",
    ),
    Row(
        "remediation_attaches_matching_generation",
        _world(ci="fail",
               runner={"live": True, "role": "remediation", "head": "same"}),
        "remediation",
        "Runner mapping: Starting or running with desired role and exact "
        "head -> Attach and wait",
        "Contrast case for remediation_fences_live_review_merge: same "
        "desired role, matching live generation at the exact head -> attach "
        "instead of fencing (effect differs, route/reason do not).",
        "scar",
    ),
    Row(
        "remediation_fences_stale_head_matching_role",
        _world(ci="fail",
               runner={"live": True, "role": "remediation", "head": "stale"}),
        "remediation",
        "Required regression matrix: a stale-head runner is never attached",
        "Role matches but head is stale -- must still fence, proving head "
        "alone (not just role) gates attach.",
        "scar",
    ),
    Row(
        "review_merge_attaches_matching_generation",
        _world(review="missing",
               runner={"live": True, "role": "review_merge", "head": "same"}),
        "review_merge",
        "Runner mapping: Starting or running with desired role and exact "
        "head -> Attach and wait",
        "Desired role review_merge, live generation already review_merge at "
        "the exact head -> attach, not a fresh start_task.",
        "scar",
    ),
    Row(
        "review_merge_fences_wrong_role",
        _world(review="missing",
               runner={"live": True, "role": "remediation", "head": "same"}),
        "review_merge",
        "Runner mapping: Running with wrong role -> fence exact generation, "
        "then start desired role",
        "Desired role review_merge, but the live generation is a "
        "remediation runner -- wrong role must fence even though the head "
        "matches.",
        "scar",
    ),

    # --- G. Board evidence (COORD-78) --------------------------------------
    #
    # Every row below has a clean PR world on purpose. `_finding_decision` is
    # evaluated AFTER draft, exact-head CI and review in
    # `_classify_completion_base`, so evidence findings only ever decide when
    # the PR itself is green -- which is exactly the state COORD-57 was in for
    # 50 attempts. A red-CI evidence row would grade the CI branch and say
    # nothing about evidence.
    Row(
        "evidence_derived_from_exact_head_ci_receipt",
        _world(evidence={"executed_test_run": "missing"}),
        "review_merge",
        "ADR-0008 / ENFORCE-16: CI on the exact SHA is the executor of record "
        "for 'the tests ran'",
        "The COORD-57 shape, post-fix (PR #955): no agent-recorded "
        "executed_test_run, but a green external CI run pinned to the exact "
        "gated head. The gate derives the verdict and reports it as a "
        "NON-BLOCKING finding, so completion proceeds to enqueue instead of "
        "dispatching an agent to re-type a fact the server already holds.",
        "scar",
    ),
    Row(
        "evidence_missing_no_ci_receipt",
        _world(evidence={"executed_test_run": "missing", "external_ci": "none"}),
        "coordination_retry",
        "#859: evidence is derived from a real run or not at all",
        "The COORD-57 shape as it actually ran: nothing to derive from, so "
        "the refusal stands. This is the negative control for the row above "
        "-- without it, 'derives' and 'never demanded it' are "
        "indistinguishable.",
        "scar",
    ),
    Row(
        "evidence_ci_receipt_other_head_refused",
        _world(evidence={"executed_test_run": "missing",
                         "external_ci": "green_other_head"}),
        "coordination_retry",
        "ENFORCE-16: the selected run's source_sha must match the gated head "
        "exactly",
        "A green run on a DIFFERENT head must not authorize this head. The "
        "fail-closed edge of the derivation above.",
        "scar",
    ),
    Row(
        "evidence_near_miss_key_co21",
        _world(evidence={"executed_test_run": "near_miss_key",
                         "external_ci": "none"}),
        "coordination_retry",
        "COORD-61 / COORD-51 s3.3: the refusal carries what was required and "
        "what nearly matched",
        "CO-21, 2026-07-25: a runner had executed five real suites and "
        "written them under `executed_tests`, one key away from the "
        "contract, inside its Work Session hygiene. The repair dispatch was "
        "handed only the reason code, wrote nothing, and exited. This row "
        "pins the named near-miss key onto the decision, not just the "
        "reason_code -- the reason_code was already correct during the "
        "outage.",
        "scar",
    ),
    Row(
        "evidence_test_run_failed",
        _world(evidence={"executed_test_run": "failed", "external_ci": "none"}),
        "coordination_retry",
        "code_strict completion_evidence requires a PASSING executed test run",
        "A recorded run that did not pass is invalid evidence, not absent "
        "evidence -- distinct reason_code, same family.",
        "matrix",
    ),
    Row(
        "evidence_test_run_stale_head",
        _world(evidence={"executed_test_run": "stale_head",
                         "external_ci": "none"}),
        "coordination_retry",
        "Exact-head contract: evidence for another commit cannot authorize "
        "this one",
        "The run is real and passing but pinned to a different head than the "
        "Work Session -- the same staleness rule the CI receipt derivation "
        "enforces, on the agent-recorded path.",
        "matrix",
    ),
    Row(
        "evidence_work_session_missing",
        _world(evidence={"work_session": "missing"}),
        "coordination_retry",
        "code_strict: merge_requires_work_session",
        "No canonical Work Session for the task and none on the branch at "
        "this head -- the gate refuses, and the refusal is bounded "
        "coordination work rather than a human handoff.",
        "matrix",
    ),
    Row(
        "evidence_work_session_borrowed",
        _world(evidence={"work_session": "borrowed"}),
        "review_merge",
        "BUG-176: a Work Session proves workspace hygiene for a branch at a "
        "head, which is a property of the commit",
        "A co-resolved task on a PR whose work happened in ANOTHER task's "
        "session on the same branch and head. The borrow is legitimate and "
        "must not block: PR #859 had to be landed by operator bypass because "
        "it did. Auditable via work_session_borrowed_from_task.",
        "scar",
    ),
    Row(
        "evidence_review_verdict_missing",
        _world(evidence={"review_verdict": "missing"}),
        "review_merge",
        "COORD-18 exact-head review gate; ADR-0021 permits self-certification",
        "No verdict has EVER been recorded for this exact head. Routes to "
        "review_merge, not human: per the safe-merge protocol the same agent "
        "may record its own passing verdict, so waiting for an independent "
        "reviewer is the wrong move.",
        "matrix",
    ),
    Row(
        "evidence_review_verdict_stale",
        _world(evidence={"review_verdict": "stale"}),
        "review_merge",
        "COORD-18: a verdict is valid only for the exact head it was recorded "
        "against",
        "A passing verdict exists, but for a different head than the board "
        "records as current -- re-review, do not escalate.",
        "matrix",
    ),
    Row(
        "evidence_review_verdict_open_findings",
        _world(evidence={"review_verdict": "open_findings"}),
        "human",
        "COORD-18: open findings at the gated head block merge",
        "A passing verdict carrying an unresolved finding. Unlike a missing "
        "verdict this is not self-serviceable by re-review, so it leaves the "
        "review_merge lane.",
        "matrix",
    ),
    Row(
        "evidence_review_verdict_not_passed",
        _world(evidence={"review_verdict": "not_passed"}),
        "human",
        "COORD-18: pass is required",
        "A recorded changes_requested verdict at the exact head. Contrast "
        "with changes_requested_judgment_human, which drives the same "
        "conclusion through the PR review payload instead of the board's "
        "verdict store -- two independent surfaces, one policy.",
        "matrix",
    ),
]

_seen_ids = [row.id for row in CATALOG]
assert len(_seen_ids) == len(set(_seen_ids)), "duplicate catalog id"


# ---------------------------------------------------------------------------
# Unreachable axes -- documented, not silently skipped.
# ---------------------------------------------------------------------------

UNREACHABLE_AXES: list[dict[str, str]] = [
    {
        "topic": "STALE / STARTUP_FAILURE CI check states as distinct gold "
                 "rows (route=coordination_retry)",
        "reason": (
            "world.ci now accepts cancelled/stale/startup_failure and "
            "clean_ci_cancelled locks the cancelled reason. stale and "
            "startup_failure share the same coordination_retry route; "
            "add dedicated gold rows only if operators need the distinct "
            "reason codes in the scoreboard."
        ),
    },
]


# ---------------------------------------------------------------------------
# Execution.
# ---------------------------------------------------------------------------


def run_world(scenario_id: str, world: dict[str, Any]) -> dict[str, Any]:
    """Drive the real two-tick production path for one world, hermetically.

    Delegates to ``tests.conformance._gold_shared.run_world`` so the builder
    and ``gold_decisions.py`` cannot drift onto two different notions of
    "ran the tick." Returns the raw first-tick result instead of asserting
    against an ``expect`` -- the expect is what the caller is deciding
    whether to write.
    """
    return _gold_shared.run_world(scenario_id, world)


def build() -> dict[str, Any]:
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    for stale in GOLD_DIR.glob("*.json"):
        stale.unlink()

    gold_rows: list[dict[str, Any]] = []
    mismatch_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []

    for row in CATALOG:
        try:
            first = run_world(row.id, row.world)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the build
            error_rows.append({
                "id": row.id, "spec_route": row.spec_route, "error": str(exc),
            })
            continue

        decision = first["decision"]
        plan = first["plan"]
        actual_route = str(decision.get("route") or "")
        if actual_route != row.spec_route:
            mismatch_rows.append({
                "id": row.id,
                "spec_route": row.spec_route,
                "actual_route": actual_route,
                "actual_reason_code": decision.get("reason_code"),
                "actual_terminal": _shared.terminal_for(first),
                "spec_source": row.spec_source,
                "note": row.note,
            })
            continue

        actual_roles = (
            [decision["desired_role"]] if decision.get("desired_role") else []
        )
        # Locked from the observed tick through the SAME projection
        # gold_decisions.py re-checks with, so the builder cannot write a field
        # the gate does not compare (or omit one it does). COORD-78 added
        # missing_artifact_near_miss_keys through that projection.
        expect = _gold_shared.decision_expect(first)
        assert expect["route"] == actual_route
        assert expect["role_sequence"] == actual_roles
        assert expect["effect"] == plan.get("effect")
        assert expect["reason_code"] == decision.get("reason_code")
        scenario_doc = {
            "schema": _shared.SCHEMA,
            "id": row.id,
            "expect": expect,
            "world": row.world,
            "timing": TIMING,
        }
        _shared.validate_scenario(scenario_doc, f"<built:{row.id}>")
        path = GOLD_DIR / f"{row.id}.json"
        path.write_text(
            json.dumps(scenario_doc, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        gold_rows.append({
            "id": row.id,
            "category": row.category,
            "spec_route": row.spec_route,
            "spec_source": row.spec_source,
            "expect": expect,
        })

    return {
        "gold": gold_rows,
        "mismatch": mismatch_rows,
        "error": error_rows,
    }


# ---------------------------------------------------------------------------
# GOLD_COVERAGE.md
# ---------------------------------------------------------------------------

_PRIORITY_NOTE = """## Priority assumption when multiple axes are dirty

The production classifier (`src/switchboard/domain/completion/state_machine.py`,
`classify_completion`) evaluates a fixed, hand-ordered sequence of early
returns -- it does not run every check and pick the "worst" one by a generic
severity table. In the order actually checked:

1. Terminal board provenance (`Done`/`Cancelled` with valid provenance) ->
   `none`.
2. Canonical merge evidence (provenance, PR state, or `pr.merged`) ->
   `reconcile`.
3. `CLOSED` unmerged PR -> `coordination_retry` (authorized reopen) or
   `human`.
4. Missing/mismatched PR identity or head vs. the board's recorded git state
   -> `coordination_retry`.
5. **Merge conflict / DIRTY mergeability** (`_merge_conflict_decision`) --
   checked *before* queue, review, and CI. An agent-fixable rebase always
   outranks queue and CI coordination noise (BUG-182): conflict wins over a
   red CI check, over a queued merge-group entry, over a missing review.
6. Merge-queue already merged -> `reconcile`.
7. Exact-head `CHANGES_REQUESTED` (`_changes_requested_decision`) -- checked
   *before* queue wait/unmergeable and before CI, but *after* conflict and
   queue-merged. A durable, actionable review verdict outranks nonterminal
   queue/CI state.
8. Merge-queue wait states (`queued`/`awaiting_checks`/`mergeable`/`locked`)
   and `unmergeable` -- checked *before* required-CI. The queue's own
   attribution of an `unmergeable` failure (product vs. infrastructure vs.
   policy) decides the route without ever consulting the CI checks
   underneath it.
9. Required exact-head CI *failures* that route `remediation` or `human`
   (`_required_ci_decision`) -- product/authority red CI still outranks
   draft (Invariant 7 / `draft_red_ci`).
10. **Draft mark-ready** -- when `pr.draft` is true and CI did not already
    force remediation/human, return `draft_ready_to_mark_ready`. This
    outranks pending/hydration CI wait so Autopilot tip reason cannot hide
    BREAKDOWN 5 behind `required_exact_head_ci_pending` (COORD-56 /
    `draft_pending_mark_ready`).
11. Remaining required-CI wait / coordination_retry outcomes (pending,
    cancelled, hydration missing).
12. Review missing/stale -> `review_merge`.
13. Merge-gate coded findings (`_finding_decision`) -- human / review /
    remediation / coordination_retry findings, keyed off the finding's own
    `code`/`failure_class`/`finding_class`, selected by
    `_select_decision`'s route-then-reason_code-then-role total order so
    source ordering can never pick the winner.
14. Mergeability aggregate decomposition (`BEHIND` -> `coordination_retry`,
    `UNKNOWN`/`mergeable is None` -> `wait`). `BLOCKED`/`UNSTABLE` are
    intentionally *not* checked here -- they fall through as a no-op,
    which is exactly the spec's "never route directly from this aggregate
    alone" (see `aggregate_blocked_not_masking_green`).
15. Live-runner fencing (role AND exact head must both match the desired
    role for the current route, or the generation is fenced and replaced;
    Invariant 5) -- evaluated last, once the desired route/role are already
    known, per "Required precedence" in the spec doc.

Net effect for this catalog: **conflict/DIRTY beats a failed required CI
check**, **an `unmergeable` queue's own attribution beats the CI axis
entirely**, and **draft mark-ready beats pending CI wait** while still
losing to red product CI. `queue_unmergeable_product` /
`queue_unmergeable_infra` set both `ci` and `queue` dirty at once to prove
the queue's attribution wins; `draft_pending_mark_ready` proves draft is
not masked by CI lag; `merge_conflict` never touches CI at all because the
conflict check does not look at it.
"""


def render_coverage_doc(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Gold decision-quality catalog coverage")
    lines.append("")
    lines.append(
        "Generated by `scripts/completion_conformance/build_gold_catalog.py`. "
        "Do not hand-edit -- rerun the builder instead. Grades Autopilot "
        f"completion routing against `{SPEC_DOC}`, not against the "
        "classifier's own output: a row is `gold` only because its route was "
        "predicted by the spec *before* the tick ran."
    )
    lines.append("")
    lines.append(
        f"gold={len(result['gold'])} spec_mismatch={len(result['mismatch'])} "
        f"error={len(result['error'])} "
        f"unreachable_in_t1_axes={len(UNREACHABLE_AXES)}"
    )
    lines.append("")
    lines.append(_PRIORITY_NOTE)
    lines.append("")
    lines.append("## Catalog")
    lines.append("")
    lines.append("| scenario_id | spec_route | status | spec_source |")
    lines.append("|---|---|---|---|")
    by_id = {row.id: row for row in CATALOG}
    for row in CATALOG:
        if any(g["id"] == row.id for g in result["gold"]):
            status = "gold"
        elif any(m["id"] == row.id for m in result["mismatch"]):
            status = "**SPEC_MISMATCH**"
        else:
            status = "**ERROR**"
        source = by_id[row.id].spec_source.replace("|", "\\|")
        lines.append(f"| `{row.id}` | `{row.spec_route}` | {status} | {source} |")
    lines.append("")

    if result["mismatch"]:
        lines.append("## SPEC_MISMATCH detail")
        lines.append("")
        lines.append(
            "Each row below is a real gap between "
            f"`{SPEC_DOC}` and the production classifier -- the observed "
            "route diverged from the spec route, so it was **not** promoted "
            "to gold."
        )
        lines.append("")
        for m in result["mismatch"]:
            lines.append(f"### `{m['id']}`")
            lines.append("")
            lines.append(f"- spec source: {m['spec_source']}")
            lines.append(f"- spec route: `{m['spec_route']}`")
            lines.append(f"- actual route: `{m['actual_route']}`")
            lines.append(f"- actual reason_code: `{m['actual_reason_code']}`")
            lines.append(f"- actual terminal: `{m['actual_terminal']}`")
            lines.append(f"- note: {m['note']}")
            lines.append("")

    if result["error"]:
        lines.append("## Build errors")
        lines.append("")
        lines.append(
            "Raised while driving the tick itself (e.g. an idempotency "
            "invariant broke) -- distinct from a spec/route mismatch."
        )
        lines.append("")
        for e in result["error"]:
            lines.append(f"- `{e['id']}` (spec route `{e['spec_route']}`): {e['error']}")
        lines.append("")

    lines.append("## unreachable_in_t1_axes")
    lines.append("")
    lines.append(
        "Scenarios the state-machine doc calls for, that this world/scenario "
        "schema (`tests/conformance/scenario.schema.json` + "
        "`tests/conformance/_shared.py`) cannot express without first "
        "extending an axis. Listed here rather than silently skipped."
    )
    lines.append("")
    for item in UNREACHABLE_AXES:
        lines.append(f"- **{item['topic']}**: {item['reason']}")
    lines.append("")

    lines.append("## How to run")
    lines.append("")
    lines.append("```bash")
    lines.append("python3.11 scripts/completion_conformance/build_gold_catalog.py")
    lines.append("python3.11 tests/conformance/gold_decisions.py")
    lines.append("# CI merge gate (auto-discovered by switchboard_ci.sh):")
    lines.append("python3.11 tests/conformance/test_gold_decisions.py")
    lines.append("```")
    lines.append("")
    lines.append(
        "The builder is re-runnable: every run wipes "
        "`tests/conformance/scenarios/gold/*.json` and this file, then "
        "regenerates both from the catalog above. `test_gold_decisions.py` is "
        "merge-gated via `switchboard_ci.sh` discovery and delegates to "
        "`gold_decisions.py`. It is intentionally separate from "
        "`test_completion_conformance.py` (T1 seed / axis-coverage) so neither "
        "suite's stability depends on the other."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    result = build()
    COVERAGE_DOC.write_text(render_coverage_doc(result), encoding="utf-8")

    for g in result["gold"]:
        print(f"GOLD  {g['id']}  route={g['spec_route']}  "
              f"effect={g['expect']['effect']}  reason={g['expect']['reason_code']}")
    for m in result["mismatch"]:
        print(f"SPEC_MISMATCH  {m['id']}  spec={m['spec_route']}  "
              f"actual={m['actual_route']}  reason={m['actual_reason_code']}")
    for e in result["error"]:
        print(f"ERROR  {e['id']}  {e['error']}")

    print(
        f"\nBuilt {len(result['gold'])} gold, {len(result['mismatch'])} "
        f"spec_mismatch, {len(result['error'])} error; "
        f"{len(UNREACHABLE_AXES)} axes documented unreachable.\n"
        f"Coverage doc: {COVERAGE_DOC.relative_to(ROOT)}\n"
        f"Gold dir: {GOLD_DIR.relative_to(ROOT)}"
    )
    return 1 if (result["mismatch"] or result["error"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
