#!/usr/bin/env python3
"""BUG-81: deploy proof uses the runtime's canonical readiness identity."""
from __future__ import annotations

import json
import subprocess
import sys

from path_setup import ROOT

INVENTORY = ROOT / "deploy" / "service-cut-inventory.json"
RENDERER = ROOT / "scripts" / "service_cut_inventory.py"

inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
monolith = next(row for row in inventory["services"] if row["name"] == "projectplanner")
assert monolith["runtime_identity"] == "taikun-pm"

rendered = subprocess.run(
    [sys.executable, str(RENDERER), "shell", "--inventory", str(INVENTORY)],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=True,
).stdout
proof_ready = next(line for line in rendered.splitlines() if line.startswith("PROOF_READY="))

assert "taikun-pm:8110:/health/deep" in proof_ready
assert "projectplanner:8110:/health/deep" not in proof_ready
cut_services = " ".join(
    row["name"] for row in sorted(inventory["services"], key=lambda row: row["restart_order"])
    if row["name"] != "projectplanner"
)
assert f"CUT_SERVICES=({cut_services})" in rendered

print("BUG-81 runtime readiness identity: 4 passed, 0 failed")
