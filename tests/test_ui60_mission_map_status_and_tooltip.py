#!/usr/bin/env python3
"""UI-60: status-only mission map colors + narration on mission tooltips."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="ui60-mission-map-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import mission_graph  # noqa: E402
import store  # noqa: E402
from switchboard.application.queries import task_session  # noqa: E402

# Reload so a prior imported store (other tests) picks up this module's DB paths.
store = importlib.reload(store)
store.init_project_registry()
store.init_db("switchboard")


class StatusOnlyMapColorTest(unittest.TestCase):
    def test_in_progress_stays_blue_despite_start_failed_honest_display(self):
        honest = task_session.display_projection({
            "lifecycle_phase": "start_failed_retry",
            "active_runner": None,
            "last_dispatch_outcome": {
                "state": "launch_failed",
                "reason": "capacity exhausted",
                "retry_available": True,
            },
            "task": {"status": "In Progress"},
        })
        self.assertEqual(honest.get("graph_state"), "start_failed")
        self.assertEqual(
            mission_graph.node_execution_state({
                "status": "In Progress",
                "honest_display": honest,
                "lifecycle_phase": "start_failed_retry",
                "provenance": {},
            }),
            "in_progress",
        )

    def test_classic_status_buckets(self):
        cases = [
            ("Not Started", "todo"),
            ("Blocked", "blocked"),
            ("In Progress", "in_progress"),
            ("In Review", "in_review"),
            ("Done", "done_unproven"),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(
                    mission_graph.node_execution_state({"status": status, "provenance": {}}),
                    expected,
                )


class MissionTooltipNarrationEnrichTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_batch_enrich_includes_task_narration_for_tooltips(self):
        task = store.create_task(
            {
                "workstream_id": "UI60",
                "title": "Tooltip narration carrier",
                "status": "In Progress",
                "description": "Must surface CEO prose on the mission map hover.",
            },
            actor="ui60-test",
            project="switchboard",
        )
        prose = "The agent is proving provider parity end-to-end on the live board."
        fp = store.task_narration_fingerprint(store.get_task(
            task["task_id"], project="switchboard"))
        store.set_task_narration(
            task["task_id"], prose, activity_cursor=0,
            source_fingerprint=fp, model="test", project="switchboard",
        )
        link = store._enriched_mission_task_link({
            "project_id": "switchboard",
            "task_id": task["task_id"],
            "blocks_deliverable": True,
            "metadata": {},
            "role": "implementation",
        })
        detail = (link or {}).get("task_detail") or {}
        self.assertEqual(detail.get("narration"), prose)
        self.assertFalse(detail.get("narration_stale"))


class MissionLiveSignatureSourceTest(unittest.TestCase):
    def test_mission_js_signature_includes_narration_and_agents(self):
        src = (ROOT / "static" / "js" / "mission.js").read_text(encoding="utf-8")
        body = src.split("_missionSignature()")[1].split("_missionLiveStamp")[0]
        self.assertIn("narration", body)
        self.assertIn("active_agents", body)
        self.assertNotIn(
            "['start_failed', 'Start failed / Retry'",
            src,
        )


if __name__ == "__main__":
    unittest.main()
