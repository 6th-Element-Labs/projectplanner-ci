"""BUG-194 — pin the merge-gate finding shape so consumers stop guessing it.

``_merge_gate_finding`` takes a parameter called ``details`` and SPLATS it onto the
finding. There is no ``finding["details"]``. The name says nested, the behaviour is flat,
and on 2026-07-25 two unrelated fixes by two different authors were both written against
the name instead of the behaviour:

* COORD-61's ``_missing_artifact_identity`` read ``finding["details"]`` and returned {}
  for every real finding, so the missing-artifact report the gate wrote never reached
  ``features_json``.
* BUG-182's ``_merge_conflict_decision`` read ``finding["details"]`` for the conflict
  evidence, so the branch it exists to reach — "PR hydration is empty, the finding is the
  only evidence left" — never fired, and merge conflicts kept being reported as a missing
  PR on bounded infra retry. That is the 92-tick defect BUG-182 was filed for, shipped
  with a fix that could not work.

Both shipped green because both test suites hand-built the nested shape. A test that
constructs its own fixture is only testing the author's belief; only the gate's own
constructor tests the system.

Mission Bot replaced the old classifier helpers. These tests pin the splat shape (§1),
prove Mission Bot facts/dossier consumers read it correctly (§2), and make a silent
recurrence impossible by failing on the nested-details pattern itself (§3).
"""
from __future__ import annotations

import ast
import pathlib
import unittest

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands.merge_gate import _merge_gate_finding
from switchboard.domain.completion.state_machine import (
    build_completion_snapshot,
    classify_completion,
)
from switchboard.domain.mission_bot import facts
from switchboard.domain.mission_bot.dossier import _missing_artifact_from_findings


HEAD = "8a79c572037366f1b100531441b19908c4717b51"
PR_URL = "https://github.com/6th-Element-Labs/projectplanner/pull/863"


# Every detail-carrying blocking finding the gate emits, as the gate emits it.
def _finding(code, message, **details):
    return _merge_gate_finding(code, message, "failed_gate", details=details or None)


def _finding_detail(finding, *keys):
    """Read a splat (or legacy nested) finding detail — dual-shape accessor.

    Kept local to this contract test: Mission Bot production code reads flat
    fields directly (``finding.get("mergeable")``), and dossier lift reads
    splat ``executed_test_gate`` the same way. The dual-shape helper documents
    the trap for any future consumer.
    """
    if not isinstance(finding, dict):
        return None
    nested = finding.get("details")
    nested = nested if isinstance(nested, dict) else {}
    for key in keys:
        if key in finding:
            return finding[key]
        if key in nested:
            return nested[key]
    return None


# ---------------------------------------------------------------------------
# §1 the shape itself
# ---------------------------------------------------------------------------

class FindingShapeIsFlatTest(unittest.TestCase):
    def test_details_are_splatted_not_nested(self):
        finding = _finding(
            "pr_not_mergeable", "GitHub PR state is not cleanly mergeable.",
            mergeable=False, merge_state="dirty")
        self.assertNotIn(
            "details", finding,
            "the gate splats details; a consumer reading finding['details'] gets "
            "nothing. If this ever nests, every finding-detail caller must be revisited",
        )
        self.assertIs(finding["mergeable"], False)
        self.assertEqual(finding["merge_state"], "dirty")

    def test_a_finding_without_details_carries_only_the_envelope(self):
        self.assertEqual(
            set(_merge_gate_finding("review_required", "m", "failed_gate")),
            {"code", "message", "failure_class", "severity", "blocking"},
        )

    def test_the_constructor_documents_the_trap(self):
        # The signature is the trap. If the warning is deleted, the next author gets
        # exactly the same surprise the last two did.
        doc = (_merge_gate_finding.__doc__ or "").lower()
        self.assertIn("splat", doc)
        self.assertIn("top level", doc)


# ---------------------------------------------------------------------------
# §2 Mission Bot consumers through the splat shape
# ---------------------------------------------------------------------------

class FindingDetailAccessorTest(unittest.TestCase):
    def test_it_reads_the_flat_shape_the_gate_emits(self):
        finding = _finding("pr_not_mergeable", "m", mergeable=False,
                           merge_state="dirty")
        self.assertIs(_finding_detail(finding, "mergeable"), False)
        self.assertEqual(_finding_detail(finding, "merge_state"), "dirty")

    def test_it_still_reads_a_nested_finding(self):
        # Preflight findings (work_sessions.py) genuinely nest under "details".
        nested = {"code": "pr_not_mergeable",
                  "details": {"mergeable": False, "merge_state": "dirty"}}
        self.assertIs(_finding_detail(nested, "mergeable"), False)
        self.assertEqual(_finding_detail(nested, "merge_state"), "dirty")

    def test_it_tries_each_alias_in_order(self):
        finding = _finding("pr_not_mergeable", "m", mergeStateStatus="DIRTY")
        self.assertEqual(
            _finding_detail(finding, "merge_state", "mergeStateStatus"), "DIRTY")

    def test_a_missing_detail_is_none_not_an_error(self):
        self.assertIsNone(_finding_detail({}, "mergeable"))
        self.assertIsNone(_finding_detail({"details": "not-a-mapping"}, "mergeable"))


class ConsumersReadRealFindingsTest(unittest.TestCase):
    """Mission Bot facts/dossier consumers, driven by the gate's own constructor."""

    def test_the_conflict_decomposer_fires_on_a_finding_only_conflict(self):
        """BUG-182's whole purpose: PR hydration empty, finding is the only evidence."""
        finding = _finding(
            "pr_not_mergeable",
            "GitHub PR state is not cleanly mergeable.",
            mergeable=False, merge_state="dirty",
        )
        self.assertTrue(
            facts.merge_conflict({"github_pr": {}, "findings": [finding]}),
            "a conflicted PR must reach pr_merge_conflict, not read as missing",
        )
        snap = build_completion_snapshot(
            task={
                "task_id": "CO-20",
                "status": "In Review",
                "git_state": {
                    "head_sha": HEAD, "pr_number": 863, "pr_url": PR_URL,
                },
            },
            github_pr={},
            review={
                "status": "passed", "head_sha": HEAD, "number": 863,
                "pr_url": PR_URL,
            },
            merge_gate={
                "findings": [finding],
                "head_sha": HEAD,
                "pr_number": 863,
                "pr_url": PR_URL,
            },
        )
        snap["head_sha"] = HEAD
        decision = classify_completion(None, snap)
        self.assertEqual(decision["reason_code"], "pr_merge_conflict")
        self.assertEqual(decision["route"], "remediation")

    def test_the_artifact_lift_fires_on_a_real_evidence_finding(self):
        finding = _merge_gate_finding(
            "missing_executed_test_run", "m", "missing_data",
            details={"executed_test_gate": {"missing_artifact": {
                "expected_key": "executed_test_run"}}})
        self.assertEqual(
            _missing_artifact_from_findings([finding]),
            {"expected_key": "executed_test_run"})

    def test_a_clean_pr_produces_no_conflict_decision(self):
        self.assertFalse(
            facts.merge_conflict({"github_pr": {"mergeable": True}, "findings": []})
        )
        self.assertFalse(facts.merge_conflict({"github_pr": {}, "findings": []}))


# ---------------------------------------------------------------------------
# §3 the guard — the pattern itself is what recurs
# ---------------------------------------------------------------------------

_COMPLETION_PACKAGE = pathlib.Path(ROOT) / "src" / "switchboard" / "domain" / "completion"
_MISSION_BOT_PACKAGE = pathlib.Path(ROOT) / "src" / "switchboard" / "domain" / "mission_bot"

#: Review evidence may carry a nested details blob; finding splat readers must not.
_DETAILS_READERS_ALLOWED = {
    "_finding_detail",
    "evidence_identity",  # review.get("details"), not merge-gate findings
}


def _direct_details_reads(path: pathlib.Path) -> list[str]:
    """Every ``<expr>.get("details")`` outside the sanctioned accessor."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in _DETAILS_READERS_ALLOWED:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if not isinstance(func, ast.Attribute) or func.attr != "get":
                continue
            if not inner.args:
                continue
            first = inner.args[0]
            if isinstance(first, ast.Constant) and first.value == "details":
                offenders.append(f"{path.name}:{inner.lineno} in {node.name}()")
    return offenders


class NobodyReadsFindingDetailsByHandTest(unittest.TestCase):
    def test_the_completion_domain_goes_through_the_accessor(self):
        """Fail on the pattern, not just on its consequences.

        Two correct call sites do not stop a third author writing
        ``finding.get("details")`` in a new consumer — it reads perfectly, matches the
        constructor's parameter name, and silently returns nothing. This is the only
        check that catches that before it ships.
        """
        offenders: list[str] = []
        for package in (_COMPLETION_PACKAGE, _MISSION_BOT_PACKAGE):
            for path in sorted(package.glob("*.py")):
                offenders.extend(_direct_details_reads(path))
        self.assertEqual(
            offenders, [],
            "read merge-gate finding details from the splat top level "
            "(Mission Bot facts/dossier); "
            "_merge_gate_finding splats them, so .get('details') is always empty. "
            f"Offenders: {offenders}",
        )

    def test_the_guard_actually_detects_the_pattern(self):
        # A guard that cannot fail is not a guard.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            probe = pathlib.Path(tmp) / "probe.py"
            probe.write_text(
                "def consume(finding):\n"
                "    return (finding.get('details') or {}).get('mergeable')\n",
                encoding="utf-8",
            )
            self.assertEqual(len(_direct_details_reads(probe)), 1)


if __name__ == "__main__":
    unittest.main()
