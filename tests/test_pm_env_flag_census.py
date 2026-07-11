#!/usr/bin/env python3
"""ARCH-MS-10: tracked PM_* declarations must have runtime defenders."""
import json
from pathlib import Path
import subprocess
import tempfile

from path_setup import ROOT

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

with tempfile.TemporaryDirectory(prefix="pm-flag-census-") as tmp:
    fixture = Path(tmp)
    name = "PM_" + "UNUSED_FIXTURE"
    (fixture / ".env.example").write_text(f"{name}=1\n", encoding="utf-8")
    (fixture / "app.py").write_text(f"# {name} is not a runtime read\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
    subprocess.run(["git", "add", ".env.example", "app.py"], cwd=fixture, check=True)

    unread = subprocess.run(
        ["python3", str(SCRIPT), "--root", str(fixture), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unread.returncode == 1
    assert name in json.loads(unread.stdout)["unread_declarations"]

    (fixture / "app.py").write_text(
        f'import os\nvalue = os.environ.get("{name}")\n', encoding="utf-8"
    )
    defended = subprocess.run(
        ["python3", str(SCRIPT), "--root", str(fixture), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert defended.returncode == 0, defended.stderr or defended.stdout

print(
    "PM env flag census: "
    f"{report['summary']['tracked_names']} tracked, "
    f"{report['summary']['declared']} declared, "
    "0 unread declarations"
)
