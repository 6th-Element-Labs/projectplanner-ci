"""BUG-247 — every systemd unit kind the repo carries must be synced by redeploy.

Origin: the BUG-245 incident response found /etc/systemd/system with NO .slice
files while deploy/ carried two — the batch tier's PERF-4 CPU/IO/memory caps had
silently never been live in production. redeploy.sh's unit-sync step copied only
``*.service`` and ``*.timer``, which is exactly the "repo carries the config, the
live copy never updated" failure the script's own header says it exists to
prevent. This test pins the sync line to every unit suffix present in deploy/.
"""
from __future__ import annotations

import re
import unittest

from path_setup import ROOT


class RedeployUnitSyncCoversEveryUnitKindTest(unittest.TestCase):
    def test_unit_sync_line_covers_every_unit_suffix_in_deploy(self):
        redeploy = (ROOT / "deploy" / "redeploy.sh").read_text(encoding="utf-8")
        sync_lines = [
            line for line in redeploy.splitlines()
            if "cp deploy/*.service" in line
        ]
        self.assertEqual(
            len(sync_lines), 1,
            "expected exactly one unit-sync cp line in redeploy.sh")
        sync = sync_lines[0]
        suffixes = {
            path.suffix for path in (ROOT / "deploy").iterdir()
            if path.suffix in {".service", ".timer", ".slice"}
        }
        for suffix in sorted(suffixes):
            self.assertIn(
                f"deploy/*{suffix}", sync,
                f"redeploy.sh unit sync omits deploy/*{suffix} — that unit kind "
                "will silently never reach /etc/systemd/system (BUG-247)")

    def test_repo_actually_carries_slice_units(self):
        slices = sorted(p.name for p in (ROOT / "deploy").glob("*.slice"))
        self.assertIn("projectplanner-batch.slice", slices)
        self.assertIn("projectplanner-interactive.slice", slices)


if __name__ == "__main__":
    unittest.main()
