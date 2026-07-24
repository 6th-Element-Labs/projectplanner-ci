#!/usr/bin/env python3
"""Structural validation for the SIMPLIFY-26 live runtime proof."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

from path_setup import ROOT


MARKER = ROOT / "docs/evidence/live/simplify24-clean-b.md"
text = MARKER.read_text(encoding="utf-8")
match = re.search(r"```json\n(?P<payload>.*?)\n```", text, re.DOTALL)
assert match, "live proof marker must contain one JSON evidence block"
proof = json.loads(match.group("payload"))

assert proof["schema"] == "switchboard.live_acceptance_proof.v1"
task_id = proof["task_id"]
assert task_id == "SIMPLIFY-26"

# Validate relationships between the persisted facts instead of inventing
# expected session or execution identifiers in the test.
assert re.fullmatch(r"worksession-[0-9a-f]{16}", proof["work_session_id"])
execution = proof["execution"]
assert re.fullmatch(r"execlease-[0-9a-f]{20}", execution["id"])
assert isinstance(execution["generation"], int) and execution["generation"] > 0
assert execution["role"] == "implementation"
assert proof["branch"].startswith(("codex/", "claude/", "cursor/"))
assert task_id in proof["branch"]

observed_at = proof["observed_at"]
assert observed_at.endswith("Z")
datetime.fromisoformat(observed_at.removesuffix("Z") + "+00:00")
