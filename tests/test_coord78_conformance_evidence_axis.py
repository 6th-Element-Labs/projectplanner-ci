#!/usr/bin/env python3
"""COORD-78: the conformance harness models board evidence, through the real gate.

Blind spot #1 of the 2026-07-26 COORD-57 incident review. ``scenario.v1`` spoke
only PR-world — draft, CI, mergeability, review, queue, runner — and T1 pinned
``merge_gate={"findings": []}``. So the three most recent completion outages
were all in a family the harness could not express:

  * COORD-57 — 50 evidence-repair dispatches for ``missing_executed_test_run``
    while the exact-head CI receipt the gate could read was green;
  * CO-21    — five executed suites recorded one key away, under
    ``executed_tests``, and the near-miss was dropped before the repair runner;
  * BUG-176  — a co-resolved task with no session of its own, wedged on
    ``work_session_required`` for work with demonstrably clean hygiene.

This file guards the two properties that make the new axis proof rather than
decoration, because both can regress silently:

  1. the findings on a conformance snapshot come from running the REAL
     ``application.commands.merge_gate``, not from a literal in the harness;
  2. those findings keep the gate's real shape — ``_merge_gate_finding``
     SPLATS ``details``, so ``finding["details"]`` does not exist. Two authors
     independently shipped consumers that read ``finding["details"]``, green,
     and dead, because their tests hand-built the nested shape (COORD-61's
     artifact lift and BUG-182's conflict decomposer, both 2026-07-25). A
     harness that hand-built findings would make that class permanently
     invisible.

Merge-gated on purpose: ``gold_decisions.py`` is not auto-discovered by
``scripts/switchboard_ci.sh``, so the evidence catalog's spec-oracle re-check is
a local suite. This file, the three T1 seeds it references, and the convergence
tier are what actually gate a merge.
"""
from __future__ import annotations

import json
from pathlib import Path

from path_setup import ROOT, SRC  # noqa: F401

import scripts.switchboard_path  # noqa: F401

# ARCH-MS-14: no one-off sys.path mutation. `tests.conformance` imports as a
# namespace package off the shared shim's repo root, the same way
# scripts/completion_conformance reaches it. Everything the evidence axis
# exposes is re-exported on `_shared`, so `_evidence` — which caches an
# in-memory database — is never imported a second time under a second name.
from tests.conformance import _shared  # noqa: E402

from constants import MERGE_GATE_SCHEMA  # noqa: E402
from switchboard.domain.completion import classify_completion  # noqa: E402

CONFORMANCE = Path(ROOT) / "tests" / "conformance"

SCENARIO_DIR = CONFORMANCE / "scenarios"
GOLD_DIR = SCENARIO_DIR / "gold"

CLEAN_WORLD = {
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

passed = 0
failed = 0


def ok(condition: bool, label: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print("PASS  %s" % label)
    else:
        failed += 1
        print("FAIL  %s" % label)


def world(**evidence_overrides: str) -> dict:
    built = dict(CLEAN_WORLD)
    if evidence_overrides:
        built["evidence"] = {
            **_shared.default_evidence(built),
            **evidence_overrides,
        }
    return built


def snapshot_for(scenario_id: str, built_world: dict) -> dict:
    return _shared.build_snapshot({"id": scenario_id, "world": built_world})


def codes(snapshot: dict, *, blocking_only: bool = False) -> list[str]:
    return [
        str(finding.get("code"))
        for finding in snapshot.get("findings") or []
        if not blocking_only or finding.get("blocking")
    ]


def all_scenarios() -> list[dict]:
    return (
        _shared.load_scenarios(SCENARIO_DIR)
        + _shared.load_scenarios(GOLD_DIR)
    )


# ---------------------------------------------------------------------------
# 1. The findings are the real gate's output, not a harness literal.
# ---------------------------------------------------------------------------

clean = snapshot_for("coord78-clean", world())
gate = clean.get("merge_gate") or {}
ok(gate.get("schema") == MERGE_GATE_SCHEMA,
   "snapshot.merge_gate carries the production merge-gate schema")
ok(all(key in gate for key in
       ("policy", "policy_profile", "review_gate", "external_ci",
        "required_status_contexts", "work_session_required")),
   "snapshot.merge_gate carries the gate's real result keys, not just findings")
ok(gate.get("policy_profile") == "code_strict"
   and bool((gate.get("policy") or {}).get("requires_executed_tests")),
   "the gate ran under the real code_strict profile rules")
ok(codes(clean) == [],
   "a fully clean evidence world produces no findings at all")

# The gate must never write while a conformance scenario runs: record=False,
# and the activity/review-save seams are armed to raise if it ever does.
ok("review_remediation_save" not in gate,
   "conformance gate runs read-only (no review remediation save)")


# ---------------------------------------------------------------------------
# 2. Real finding SHAPE: details are splatted, never nested.
# ---------------------------------------------------------------------------

refused = snapshot_for(
    "coord78-refused",
    world(executed_test_run="missing", external_ci="none"))
finding = next(
    (f for f in refused["findings"]
     if f.get("code") == "missing_executed_test_run"),
    None,
)
ok(finding is not None,
   "a missing executed_test_run world produces the real refusal finding")
ok(bool(finding) and "details" not in finding,
   "finding has no nested 'details' key — the gate splats, and so must we")
ok(bool(finding) and isinstance(finding.get("executed_test_gate"), dict),
   "the gate's detail rides at the finding's TOP level (executed_test_gate)")
ok(bool(finding) and set(finding) >= {
       "code", "message", "failure_class", "severity", "blocking"},
   "finding carries the full _merge_gate_finding envelope")


# ---------------------------------------------------------------------------
# 3. Every evidence axis value is wired to a real, distinct gate outcome.
# ---------------------------------------------------------------------------

EXPECTED_CODES = {
    ("executed_test_run", "valid"): None,
    ("executed_test_run", "missing"): "missing_executed_test_run",
    ("executed_test_run", "near_miss_key"): "missing_executed_test_run",
    ("executed_test_run", "failed"): "invalid_executed_test_run",
    ("executed_test_run", "stale_head"): "invalid_executed_test_run",
    ("work_session", "present"): None,
    ("work_session", "missing"): "work_session_required",
    ("work_session", "borrowed"): None,
    ("review_verdict", "pass"): None,
    ("review_verdict", "missing"): "review_required",
    ("review_verdict", "stale"): "stale_review_verdict",
    ("review_verdict", "open_findings"): "open_review_findings",
    ("review_verdict", "not_passed"): "review_not_passed",
}
for (axis, value), expected in EXPECTED_CODES.items():
    overrides = {axis: value}
    if axis == "executed_test_run":
        # Isolate the agent-recorded path: a green exact-head receipt would
        # (correctly) derive the verdict and mask the refusal under test.
        overrides["external_ci"] = "none"
    snap = snapshot_for(f"coord78-{axis}-{value}", world(**overrides))
    blocking = codes(snap, blocking_only=True)
    if expected is None:
        ok(blocking == [], f"evidence {axis}={value} blocks nothing")
    else:
        ok(blocking == [expected],
           f"evidence {axis}={value} produces exactly [{expected}]")

# The external_ci axis is only observable through what it does to the
# executed-test refusal, so it is graded on that rather than on a code of its own.
EXTERNAL_CI_BLOCKS = {
    "green_exact_head": False,
    "green_other_head": True,
    "failed": True,
    "pending": True,
    "none": True,
}
for value, should_block in EXTERNAL_CI_BLOCKS.items():
    snap = snapshot_for(
        f"coord78-external-ci-{value}",
        world(executed_test_run="missing", external_ci=value))
    blocking = codes(snap, blocking_only=True)
    ok(bool(blocking) is should_block,
       f"external_ci={value} {'keeps' if should_block else 'clears'} the "
       "executed-test refusal")


# ---------------------------------------------------------------------------
# 4. COORD-57: the loop, and its death.
# ---------------------------------------------------------------------------

derived = snapshot_for(
    "coord78-coord57-derived", world(executed_test_run="missing"))
derived_finding = next(
    (f for f in derived["findings"]
     if f.get("code") == "executed_test_run_derived_from_external_ci"),
    None,
)
ok(derived_finding is not None,
   "COORD-57 shape: the derivation is recorded as a visible finding")
ok(bool(derived_finding) and derived_finding.get("blocking") is False,
   "COORD-57 shape: the derivation finding is non-blocking")
derived_decision = classify_completion(None, derived)
ok(derived_decision.get("route") == "review_merge",
   "COORD-57 shape: a derived receipt routes review_merge, not repair")
ok(derived_decision.get("reason_code") == "exact_head_gates_passed",
   "COORD-57 shape: reason is exact_head_gates_passed")

no_receipt_decision = classify_completion(None, refused)
ok(no_receipt_decision.get("route") == "remediation",
   "COORD-57 negative control: no receipt boots remediation with evidence dossier")
ok(no_receipt_decision.get("reason_code") == "missing_executed_test_run",
   "COORD-57 negative control: reason is missing_executed_test_run")
ok(derived_decision.get("route") != no_receipt_decision.get("route"),
   "the two COORD-57 shapes are distinguishable — 'derives' is not "
   "indistinguishable from 'never demanded it'")


# ---------------------------------------------------------------------------
# 5. CO-21: the near-miss key survives to the decision.
# ---------------------------------------------------------------------------

near_miss_snapshot = snapshot_for(
    "coord78-co21",
    world(executed_test_run="near_miss_key", external_ci="none"))
near_miss_decision = classify_completion(None, near_miss_snapshot)
ok(_shared.missing_artifact_near_miss_keys(near_miss_decision)
   == ["executed_tests"],
   "CO-21 shape: the decision NAMES the near-miss key, not just the reason")
report = (near_miss_decision.get("missing_artifact") or {})
ok(report.get("expected_key") == "executed_test_run",
   "CO-21 shape: the report also names the key that WAS required")
ok(report.get("repair_tool") == "record_executed_test_run",
   "CO-21 shape: the report names the one call that repairs it")
holder = [
    item.get("work_session_id")
    for item in report.get("found_near_miss") or []
]
ok(holder and all(holder),
   "CO-21 shape: the near miss names the Work Session holding it")


# ---------------------------------------------------------------------------
# 6. The axis is exercised by the scenario catalog, not just reachable in code.
# ---------------------------------------------------------------------------

coverage = _shared.evidence_coverage(all_scenarios())
ok(coverage["undefined"] == [],
   "every evidence axis value has at least one scenario: "
   f"{coverage['undefined'] or 'none undefined'}")

seeds = {s["id"]: s for s in _shared.load_scenarios(SCENARIO_DIR)}
ok("coord57_evidence_derived_from_ci_receipt" in seeds
   and "coord57_evidence_missing_no_ci_receipt" in seeds
   and "co21_evidence_near_miss_key" in seeds,
   "the incident shapes are T1 seeds (merge-gated), not gold-only")
ok(seeds.get("co21_evidence_near_miss_key", {}).get("expect", {})
   .get("missing_artifact_near_miss_keys") == ["executed_tests"],
   "the CO-21 seed asserts the named near-miss key in its expect block")


# ---------------------------------------------------------------------------
# 7. Omitted evidence agrees with the PR world it was omitted from.
# ---------------------------------------------------------------------------

for ci_state, expected_receipt in (
        ("pass", "green_exact_head"), ("fail", "failed"),
        ("error", "failed"), ("pending", "pending"), ("missing", "none")):
    derived_default = _shared.default_evidence({**CLEAN_WORLD, "ci": ci_state})
    ok(derived_default["external_ci"] == expected_receipt,
       f"a scenario with ci={ci_state} and no declared evidence gets "
       f"external_ci={expected_receipt}, not a contradictory green receipt")

legacy = [s for s in all_scenarios() if "evidence" not in s["world"]]
ok(len(legacy) >= 24,
   "the pre-COORD-78 scenarios still validate without an evidence block "
   f"(found {len(legacy)})")


# ---------------------------------------------------------------------------
# 8. The schema published to operators matches the code's vocabulary.
# ---------------------------------------------------------------------------

schema = json.loads(
    (CONFORMANCE / "scenario.schema.json").read_text(encoding="utf-8"))
schema_evidence = (
    schema["properties"]["world"]["properties"].get("evidence") or {})
schema_axes = {
    axis: tuple(spec.get("enum") or ())
    for axis, spec in (schema_evidence.get("properties") or {}).items()
}
ok(schema_axes == {axis: tuple(values)
                   for axis, values in _shared.EVIDENCE_AXES.items()},
   "scenario.schema.json world.evidence enums match _shared.EVIDENCE_AXES")
ok("evidence" not in (schema["properties"]["world"].get("required") or []),
   "world.evidence stays optional so pre-COORD-78 scenarios remain valid")

print(f"\ncoord78 conformance evidence axis: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
