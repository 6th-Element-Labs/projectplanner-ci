#!/usr/bin/env python3
"""Live mission remounts must not yank the page scroll on every poll tick.

The deliverable cockpit polls every 5s and re-renders when the signature
changes. A full #mission-page innerHTML wipe used to collapse the dependency
map (min-height only) and never restore window.scrollY — content below the
map jumped down. Keep the live clock; stop the jump.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from path_setup import ROOT

MISSION = (ROOT / "static" / "js" / "mission.js").read_text(encoding="utf-8")


class MissionLiveScrollStableTest(unittest.TestCase):
    def _render_body(self) -> str:
        marker = "    renderMissionPage() {"
        start = MISSION.find(marker)
        self.assertGreaterEqual(start, 0, "renderMissionPage definition missing")
        end = MISSION.find("    async openLinkedTask(", start)
        self.assertGreater(end, start, "openLinkedTask must follow renderMissionPage")
        return MISSION[start:end]

    def test_render_mission_page_preserves_window_scroll(self):
        body = self._render_body()
        # Capture page scroll before the wipe…
        self.assertTrue(
            "scrollY" in body or "pageYOffset" in body or "scrollingElement" in body,
            "renderMissionPage must capture window/page scroll before remount",
        )
        # …and put it back after the remount (not only the graph scroller).
        self.assertRegex(
            body,
            r"scroll(To|Top|\.scrollTop)\s*[=(]",
            "renderMissionPage must restore page scroll after remount",
        )

    def test_render_mission_page_freezes_graph_height_across_remount(self):
        body = self._render_body()
        self.assertIn("offsetHeight", body)
        self.assertIn("minHeight", body)

    def test_mission_signature_uses_compact_summary_without_heartbeat_timestamp(self):
        """Summary polling must not reintroduce heavy Autopilot heartbeat state."""
        body = MISSION.split("    _missionSignature()")[1].split("    _missionLiveStamp")[0]
        self.assertIn("missionSummary", body)
        self.assertNotIn("autopilotScopes", body)
        self.assertNotIn("updated_at", body)


if __name__ == "__main__":
    unittest.main()
