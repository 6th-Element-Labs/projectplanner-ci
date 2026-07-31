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

import unittest

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands.merge_gate import _merge_gate_finding


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


# §2-§4 (retired with SIMPLIFY-30): the v1 facts accessor and its reducer
# consumers were deleted with the Mission Bot v1 controller; §1 above remains
# the contract for the live merge gate's finding shape.


if __name__ == "__main__":
    unittest.main(verbosity=2)
