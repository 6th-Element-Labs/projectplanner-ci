import json
import unittest
from unittest.mock import patch

from path_setup import ROOT

from switchboard.storage.repositories import decision_records
from switchboard.storage.repositories import provenance


EVIDENCE = (
    ROOT / "docs/evidence/live/coord58-stale-runner-corpus-2026-07-25.json"
)


class RecordedStaleRunnerWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        assert cls.window["synthetic"] is False

    def test_replay_surfaces_both_recorded_occurrences_without_live_query(self):
        surfaced = []
        for occurrence in self.window["occurrences"]:
            findings = decision_records.stale_runner_findings_from_episodes(
                occurrence["episodes"]
            )
            self.assertTrue(findings, occurrence["label"])
            self.assertTrue(all(
                finding["live_runner_query_used"] is False for finding in findings
            ))
            surfaced.append(occurrence["label"])
        self.assertEqual(
            surfaced, ["occurrence-1-0500", "occurrence-2-0700"]
        )

    def test_signal_names_task_pinned_head_and_current_pr_head(self):
        episodes = self.window["occurrences"][1]["episodes"]
        findings = decision_records.stale_runner_findings_from_episodes(episodes)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["task_id"], "CO-20")
        self.assertEqual(
            finding["pinned_head"],
            "d150fd1b376f0b8e6c4b1e5125fb808d0b5ddcdf",
        )
        self.assertEqual(
            finding["current_pr_head"],
            "a894afc0ef8bb84dec565b7a8fe9b15b7dedaaaf",
        )
        self.assertEqual(finding["episode_count"], 5)
        self.assertEqual(finding["tick_count"], 584)
        self.assertIn(finding["task_id"], finding["detail"])
        self.assertIn(finding["pinned_head"], finding["detail"])
        self.assertIn(finding["current_pr_head"], finding["detail"])

    def test_reconcile_alerts_uses_signature_dedupe_path(self):
        finding = decision_records.stale_runner_findings_from_episodes(
            self.window["occurrences"][1]["episodes"]
        )[0]
        report = {
            "findings": [],
            "checked_at": 1784963280,
            "external_checks": {},
        }
        message = {"id": 58}
        with (
            patch.object(provenance, "reconcile", return_value=report),
            patch.object(
                decision_records,
                "find_stale_runner_signals",
                return_value=[finding],
            ),
            patch.object(
                provenance,
                "close_stale_reconcile_alert_inbox",
                return_value={
                    "closed_count": 0,
                    "message_ids": [],
                    "monitor_ids": [],
                },
            ),
            patch.object(
                provenance._store_facade(),
                "send_agent_message",
                return_value=message,
            ) as send,
            patch.object(
                provenance._store_facade(),
                "_idem_hit",
                return_value=None,
            ),
            patch.object(provenance._store_facade(), "_idem_store"),
            patch.object(provenance, "_conn") as conn,
        ):
            conn.return_value.__enter__.return_value.execute.return_value = None
            result = provenance.run_reconcile_alerts(
                project="switchboard",
                now=1784963280,
                close_stale_inbox=False,
            )
        self.assertTrue(result["alert_sent"])
        self.assertEqual(result["findings"], [finding])
        self.assertIn("signature", result)
        self.assertIn("stale_runner_corpus_signal", send.call_args.kwargs["message"])


if __name__ == "__main__":
    unittest.main()
