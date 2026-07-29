"""BUG-219 canary: the first published head must fail pending remediation."""

from pathlib import Path
import runpy


marker = runpy.run_path(Path(__file__).with_name("bug219_marker.py"))
assert marker["CANARY_STATE"] == "remediation-generation", marker["CANARY_STATE"]

print("BUG-219 remediation marker is repaired")
