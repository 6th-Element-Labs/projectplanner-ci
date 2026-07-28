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

These tests pin the behaviour (§1), prove every consumer reads it correctly (§2), and
make a silent recurrence impossible by failing on the pattern itself (§3).
"""
from __future__ import annotations

import ast
import pathlib
import unittest

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands.merge_gate import _merge_gate_finding
from switchboard.domain.mission_bot import facts
from switchboard.domain.mission_bot.dossier import build_dossier
from switchboard.domain.mission_bot.reducer import reduce_mission


# Every detail-carrying blocking finding the gate emits, as the gate emits it.
def _finding(code, message, **details):
    return _merge_gate_finding(code, message, "failed_gate", details=details or None)


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
            "nothing. If this ever nests, every _finding_detail caller must be revisited",
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
        self.assertIn("facts", doc)


# ---------------------------------------------------------------------------
# §2 the accessor, and every consumer through it
# ---------------------------------------------------------------------------

class FindingDetailAccessorTest(unittest.TestCase):
    def test_it_reads_the_flat_shape_the_gate_emits(self):
        finding = _finding("pr_not_mergeable", "m", mergeable=False,
                           merge_state="dirty")
        self.assertIs(facts.finding_detail(finding, "mergeable"), False)
        self.assertEqual(facts.finding_detail(finding, "merge_state"), "dirty")

    def test_it_still_reads_a_nested_finding(self):
        # Preflight findings (work_sessions.py) genuinely nest under "details".
        nested = {"code": "pr_not_mergeable",
                  "details": {"mergeable": False, "merge_state": "dirty"}}
        self.assertIs(facts.finding_detail(nested, "mergeable"), False)
        self.assertEqual(facts.finding_detail(nested, "merge_state"), "dirty")

    def test_it_tries_each_alias_in_order(self):
        finding = _finding("pr_not_mergeable", "m", mergeStateStatus="DIRTY")
        self.assertEqual(
            facts.finding_detail(finding, "merge_state", "mergeStateStatus"), "DIRTY")

    def test_a_missing_detail_is_none_not_an_error(self):
        self.assertIsNone(facts.finding_detail({}, "mergeable"))
        self.assertIsNone(
            facts.finding_detail({"details": "not-a-mapping"}, "mergeable")
        )


class ConsumersReadRealFindingsTest(unittest.TestCase):
    """Both live consumers, driven by the gate's own constructor."""

    def test_the_conflict_decomposer_fires_on_a_finding_only_conflict(self):
        """BUG-182's whole purpose: PR hydration empty, finding is the only evidence."""
        finding = _finding("pr_not_mergeable",
                           "GitHub PR state is not cleanly mergeable.",
                           mergeable=False, merge_state="dirty")
        decision = reduce_mission({
            "task_id": "BUG-194",
            "board_status": "In Review",
            "pr_number": 194,
            "board_pr_number": 194,
            "head_sha": "a" * 40,
            "findings": [finding],
        })
        self.assertEqual(decision["reason_code"], "pr_merge_conflict")
        self.assertEqual(decision["output"], "START_REMEDIATION")

    def test_the_artifact_lift_fires_on_a_real_evidence_finding(self):
        finding = _merge_gate_finding(
            "missing_executed_test_run", "m", "missing_data",
            details={"executed_test_gate": {"missing_artifact": {
                "expected_key": "executed_test_run"}}})
        dossier = build_dossier(
            {"task_id": "BUG-194", "findings": [finding]},
            reason_code="missing_executed_test_run",
            mission="remediate",
        )
        self.assertEqual(
            dossier["missing_artifact"],
            {"expected_key": "executed_test_run"},
        )

    def test_a_clean_pr_produces_no_conflict_decision(self):
        self.assertFalse(facts.merge_conflict({"github_pr": {"mergeable": True}}))
        self.assertFalse(facts.merge_conflict({}))


# ---------------------------------------------------------------------------
# §3 the guard — the pattern itself is what recurs
# ---------------------------------------------------------------------------

_MISSION_PACKAGE = pathlib.Path(ROOT) / "src" / "switchboard" / "domain" / "mission_bot"

#: The accessor is allowed to read the nested form; it is the thing that knows about it.
_DETAILS_READERS_ALLOWED = {
    "finding_detail",
    # This reads review.details from a mission dossier, not finding.details.
    "evidence_identity",
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
        for path in sorted(_MISSION_PACKAGE.glob("*.py")):
            offenders.extend(_direct_details_reads(path))
        self.assertEqual(
            offenders, [],
            "read merge-gate finding details via facts.finding_detail(); "
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
