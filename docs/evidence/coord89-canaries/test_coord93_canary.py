"""COORD-93 canary: generation 1 must fail until remediation repairs the marker."""

from pathlib import Path
import runpy


marker = runpy.run_path(Path(__file__).with_name("coord93_marker.py"))
assert marker["CANARY_STATE"] == "remediation-generation", marker["CANARY_STATE"]

print("COORD-93 remediation marker is repaired")
