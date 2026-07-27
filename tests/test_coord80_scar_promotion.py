import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for entry in (str(ROOT), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from switchboard.application.scar_promotion import (
    predict_route_from_spec,
    promote,
    scar_kind,
    select_scars,
)


class ScarPromotionTest(unittest.TestCase):
    def test_bad_outcomes_are_selected(self):
        self.assertEqual(scar_kind({"tick_count": 8}), "repair_loop")
        self.assertEqual(
            scar_kind({"human_intervened": True, "human_action": "operator bypass"}),
            "operator_override",
        )
        self.assertEqual(
            scar_kind({"human_intervened": True, "route": "remediation"}),
            "unexpected_human_escalation",
        )
        self.assertEqual(select_scars([{"tick_count": 1}]), [])

    def test_promoter_redacts_identity_and_is_idempotent(self):
        snapshot = {
            "schema": "switchboard.completion_snapshot.v1",
            "task_id": "SECRET-1",
            "pr_number": 999,
            "head_sha": "a" * 40,
            "github_pr": {
                "number": 999, "state": "OPEN", "draft": True,
                "url": "https://example.invalid/private/pull/999",
                "mergeable": True, "mergeStateStatus": "CLEAN",
                "head": {"sha": "a" * 40},
            },
            "required_status_contexts": ["Switchboard CI / VM gate"],
            "status_contexts": [{
                "context": "Switchboard CI / VM gate",
                "state": "SUCCESS",
                "failure_attribution": "product",
            }],
            "review": {"status": "passed", "head_sha": "a" * 40},
            "merge_gate": {"findings": []},
            "merge_queue": {},
            "runner": {"live": False},
        }
        episode = {
            "record_id": "private-record",
            "tick_count": 9,
            "route": "review_merge",
            "snapshot_retained": True,
            "snapshot": snapshot,
        }
        with tempfile.TemporaryDirectory() as tmp:
            first = promote([episode], Path(tmp))
            second = promote([episode], Path(tmp))
            self.assertEqual(first["written"], second["written"])
            payload = Path(first["written"][0]).read_text()
            self.assertNotIn("SECRET-1", payload)
            self.assertNotIn("private-record", payload)
            self.assertNotIn("example.invalid", payload)
            self.assertEqual(first["pull_request"]["ready"], True)

    def test_spec_oracle_is_independent_for_draft_pending_scar(self):
        snapshot = {"merge_queue": {}}
        world = {
            "pr_state": "open", "queue": "none", "ci": "pending",
            "ci_attribution": "product", "draft": True, "review": "passed",
            "mergeable": True, "merge_state_status": "BLOCKED",
        }
        self.assertEqual(
            predict_route_from_spec(snapshot, world), "review_merge"
        )


if __name__ == "__main__":
    unittest.main()
