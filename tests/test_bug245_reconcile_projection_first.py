"""BUG-245 — reconcile memory is bounded by matches, never by window row size.

Origin: 2026-07-30 production wedge. Pre-BUG-243 recursive Autopilot persistence
left decision_records rows with snapshot bodies up to 47 MB. The stale-runner
window query did ``SELECT *`` and ``_row()``-parsed every body — 229 MB of JSON
became ~1 GB of Python objects inside the reconcile unit's 384 MB MemoryHigh,
and the run thrashed in ``mem_cgroup_handle_over_high`` for 11 hours holding the
single-flight lock, driving box-wide memory PSI to 88% and shedding all load.

Two invariants pinned here:

1. Read side: ``find_stale_runner_signals`` touches ``snapshot_json`` only for
   episodes that already matched the projected feature flags. A non-matching
   episode's body is never fetched, never parsed — the projection boundary that
   ``export_projection`` already documents applies to this reader too.
2. Write side: ``record_decision_episode`` refuses to persist a snapshot body
   over ``DECISION_SNAPSHOT_MAX_BYTES``, storing the compaction marker instead
   (the ``autopilot_scopes`` oversized-result pattern), so one misbehaving
   producer cannot poison every future window read.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.storage.migrations import runner as migrations
from switchboard.storage.repositories import decision_records


MATCH_HEAD = "a" * 40
PINNED_HEAD = "b" * 40
POISON_PAD = "POISON-SNAPSHOT-BODY-" * 64


def _features(matching: bool) -> str:
    return json.dumps({
        "runner_live": True,
        "runner_head_matches_exact_head": not matching,
    })


class Bug245Base(unittest.TestCase):
    project = "switchboard"

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE deliverable_task_links ("
            "id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL, "
            "project_id TEXT NOT NULL, task_id TEXT NOT NULL, created_at REAL)")
        wanted = {
            "0117_decision_records",
            "0118_ix_decision_records_projection",
            "0119_ix_decision_records_convergence",
        }
        for name, sql in migrations.DDL_MIGRATIONS:
            if name in wanted:
                self.db.execute(sql)
        self.patches = [
            patch.object(decision_records, "_conn", return_value=self.db),
            patch.object(
                decision_records, "_write_through",
                side_effect=lambda _project, fn: fn()),
        ]
        for p in self.patches:
            p.start()
        self.now = 1_700_000_000.0

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()

    def _insert(self, record_id, *, matching, snapshot, task_id="QA-1",
                head_sha=MATCH_HEAD):
        self.db.execute(
            "INSERT INTO decision_records("
            "record_id, project, task_id, pr_number, head_sha, snapshot_hash, "
            "snapshot_json, decision_json, classifier_version, reason_code, "
            "features_json, features_version, tick_count, "
            "first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record_id, self.project, task_id, 7, head_sha,
                f"hash-{record_id}", json.dumps(snapshot), "{}", "test-v1",
                "required_exact_head_ci_failed", _features(matching),
                "test-features-v1", 3, self.now, self.now,
            ),
        )
        self.db.commit()


class FindStaleRunnerSignalsProjectionFirstTest(Bug245Base):
    """Read side: snapshot bodies load only for episodes that already matched."""

    def test_matching_episode_still_names_pinned_and_current_head(self):
        self._insert(
            "decision-match", matching=True,
            snapshot={"runner": {"head_sha": PINNED_HEAD}})
        findings = decision_records.find_stale_runner_signals(
            project=self.project, since=self.now - 60, until=self.now + 60)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["pinned_head"], PINNED_HEAD)
        self.assertEqual(findings[0]["current_pr_head"], MATCH_HEAD)
        self.assertEqual(findings[0]["tick_count"], 3)

    def test_nonmatching_snapshot_body_is_never_parsed(self):
        self._insert(
            "decision-match", matching=True,
            snapshot={"runner": {"head_sha": PINNED_HEAD}})
        self._insert(
            "decision-poison", matching=False,
            snapshot={"pad": POISON_PAD}, task_id="QA-2")

        real_map = decision_records._map
        seen_poison = []

        def guarded_map(value):
            if isinstance(value, str) and POISON_PAD[:32] in value:
                seen_poison.append(value)
            return real_map(value)

        with patch.object(decision_records, "_map", side_effect=guarded_map):
            findings = decision_records.find_stale_runner_signals(
                project=self.project, since=self.now - 60, until=self.now + 60)

        self.assertEqual(len(findings), 1)
        self.assertEqual(
            seen_poison, [],
            "snapshot_json of a non-matching episode reached the JSON parser; "
            "window memory is no longer bounded by matches (BUG-245)")

    def test_window_query_does_not_select_snapshot_bodies(self):
        self._insert(
            "decision-match", matching=True,
            snapshot={"runner": {"head_sha": PINNED_HEAD}})
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            decision_records.find_stale_runner_signals(
                project=self.project, since=self.now - 60, until=self.now + 60)
        finally:
            self.db.set_trace_callback(None)
        body_reads = [s for s in statements if "snapshot_json" in s]
        self.assertTrue(
            all("record_id" in s for s in body_reads),
            f"windowed statement selects snapshot_json: {body_reads}")


class RecordDecisionEpisodeSnapshotCapTest(Bug245Base):
    """Write side: an oversized snapshot body persists as the marker, not the body."""

    def _record(self, snapshot):
        return decision_records.record_decision_episode(
            project=self.project,
            snapshot=snapshot,
            decision={"reason_code": "required_exact_head_ci_failed",
                      "route": "coordination_retry", "desired_role": ""},
            classifier_version="test-v1",
            now=self.now,
        )

    def test_small_snapshot_stored_verbatim(self):
        self._record({"task_id": "QA-1", "head_sha": MATCH_HEAD, "note": "small"})
        row = self.db.execute(
            "SELECT snapshot_json, snapshot_retained FROM decision_records").fetchone()
        self.assertEqual(json.loads(row["snapshot_json"])["note"], "small")
        self.assertEqual(row["snapshot_retained"], 1)

    def test_oversized_snapshot_stored_as_marker(self):
        pad = "x" * (decision_records.DECISION_SNAPSHOT_MAX_BYTES + 1024)
        self._record({"task_id": "QA-1", "head_sha": MATCH_HEAD, "pad": pad})
        row = self.db.execute(
            "SELECT snapshot_json, snapshot_retained FROM decision_records").fetchone()
        self.assertLess(len(row["snapshot_json"]), 1024)
        marker = json.loads(row["snapshot_json"])
        self.assertIs(marker["snapshot_compacted"], True)
        self.assertGreater(
            marker["snapshot_bytes"], decision_records.DECISION_SNAPSHOT_MAX_BYTES)
        self.assertEqual(row["snapshot_retained"], 0)

    def test_oversized_snapshot_keeps_episode_collapsing(self):
        pad = "x" * (decision_records.DECISION_SNAPSHOT_MAX_BYTES + 1024)
        snapshot = {"task_id": "QA-1", "head_sha": MATCH_HEAD, "pad": pad}
        self._record(snapshot)
        self._record(snapshot)
        rows = self.db.execute(
            "SELECT tick_count FROM decision_records").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tick_count"], 2)


if __name__ == "__main__":
    unittest.main()
