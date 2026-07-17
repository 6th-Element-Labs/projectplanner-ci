#!/usr/bin/env python3
"""Execute the standalone multiprocess service-boundary probe."""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT  # noqa: E402
run = subprocess.run(
    [sys.executable, str(ROOT / "scripts/arch_ms126_boundary_probe.py")],
    cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
)
if run.returncode != 0:
    print(run.stdout)
    raise SystemExit("ARCH-MS-126 multiprocess boundary probe failed")
report = json.loads(run.stdout.strip().splitlines()[-1])
assert report["ok"] is True
assert report["sqlite"]["process_count"] >= 4
assert report["sqlite"]["journal_mode"] == "wal"
assert report["sqlite"]["transaction_integrity"] is True
assert report["resources"]["total_rss_bytes"] < 911 * 1024 * 1024
assert report["rollup_reconciliation"]["agrees"] is True
print("PASS ARCH-MS-126 multiprocess SQLite/resource/rollup boundary proof")
