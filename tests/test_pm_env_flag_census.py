#!/usr/bin/env python3
"""ARCH-MS-10: tracked PM_* declarations must have runtime defenders."""
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pm_env_flag_census.py"


completed = subprocess.run(
    ["python3", str(SCRIPT), "--check"],
    cwd=ROOT,
    check=False,
    capture_output=True,
    text=True,
)
assert completed.returncode == 0, completed.stderr or completed.stdout
report = json.loads(completed.stdout)

assert report["schema"] == "switchboard.pm_env_flag_census.v1"
assert report["summary"]["declared"] > 0
assert report["summary"]["runtime_referenced"] >= report["summary"]["declared"]
assert report["unread_declarations"] == []
assert set(report["declared"]).issubset(report["runtime_referenced"])

print(
    "PM env flag census: "
    f"{report['summary']['tracked_names']} tracked, "
    f"{report['summary']['declared']} declared, "
    "0 unread declarations"
)
