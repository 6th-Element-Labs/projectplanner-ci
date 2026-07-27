"""COORD-57: both verification paths must emit the same required contexts."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from path_setup import ROOT

from scripts.check_ci_workflow_contexts import (
    assert_workflow_contexts_match,
    emitted_status_contexts,
)


CANONICAL_VERIFY = ROOT / ".github" / "workflows" / "verify.yml"
PUBLIC_CI_VERIFY = ROOT / "tests" / "fixtures" / "projectplanner-ci-verify.yml"
REQUIRED = frozenset({
    "Switchboard CI / VM gate",
})


class RequiredContextVocabularyTest(unittest.TestCase):
    def test_canonical_and_public_ci_verify_contexts_match(self) -> None:
        assert_workflow_contexts_match(CANONICAL_VERIFY, PUBLIC_CI_VERIFY)
        self.assertEqual(emitted_status_contexts(CANONICAL_VERIFY), REQUIRED)

    def test_divergent_workflow_contexts_fail_loudly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="coord57-contexts-") as tmp_dir:
            root = Path(tmp_dir)
            left = root / "left.yml"
            right = root / "right.yml"
            left.write_text(
                "env:\n  STATUS_CONTEXT: one\n  UI_STATUS_CONTEXT: two\n",
                encoding="utf-8",
            )
            right.write_text("env:\n  STATUS_CONTEXT: one\n", encoding="utf-8")

            with self.assertRaisesRegex(AssertionError, "missing from"):
                assert_workflow_contexts_match(left, right)


if __name__ == "__main__":
    unittest.main()
