#!/usr/bin/env python3
"""UI-46: intentional human boundaries are visibly Autopilot exceptions."""
from __future__ import annotations

from pathlib import Path

from path_setup import ROOT  # noqa: F401
from switchboard.storage.repositories.deliverables import _mission_next_actions


STATIC = Path(ROOT) / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
MISSION = (STATIC / "js" / "mission.js").read_text(encoding="utf-8")
SETTINGS = (STATIC / "js" / "settings.js").read_text(encoding="utf-8")
PROJECT_ADMIN = (STATIC / "js" / "project-admin.js").read_text(encoding="utf-8")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


proposal_actions = _mission_next_actions(
    {}, [], {"id": "proposal-ui46", "status": "proposed"})
proposal = next(row for row in proposal_actions if row["action"] == "approve_breakdown")
ok(proposal.get("attention") is True and proposal.get("automatic") is False,
   "deliverable shaping remains an explicit human authority boundary")
ok(proposal.get("exception") is True
   and proposal.get("exception_kind") == "deliverable_authority",
   "deliverable approval is machine-readable as an Autopilot exception")

review_actions = _mission_next_actions({}, [{
    "project_id": "switchboard", "task_id": "UI-46", "blocks_deliverable": True,
    "role": "implementation", "task_detail": {
        "task_id": "UI-46", "title": "Exceptional review", "status": "Blocked",
        "active_claims": [], "dependency_state": {"ready": False},
        "review_remediation": {"current": {
            "remediation_id": "reviewremediation-ui46", "round_no": 4,
            "status": "escalated", "human_intervention_required": True,
            "escalate_finding_count": 0,
        }},
    },
}], None)
review = next(row for row in review_actions
              if row["action"] == "resolve_review_exception")
ok(review.get("exception") is True
   and review.get("exception_kind") == "review_authority"
   and review.get("delivery_impact") == "blocking",
   "exhausted review remediation becomes a blocking Autopilot exception")
ok("round 4" in review.get("reason", ""),
   "the exception explains which automatic remediation boundary was reached")

ok("Autopilot exceptions — your authority is required" in APP
   and "Routine execution remains automatic" in APP,
   "the mission queue distinguishes exceptional authority from routine automation")
ok("taskAutopilotExceptionHtml" in APP
   and "Routine implementation, testing, review, merge, and reconciliation" in APP,
   "task details explain exceptional review without implying ordinary approval gates")
ok("Waiving a security or evidence requirement always requires explicit human authority" in APP,
   "the merge surface labels security and evidence waivers as Autopilot exceptions")
ok("Human-gated.</span> This task is blocking" not in APP,
   "blocking work is no longer mislabeled as inherently human-gated")
ok("Autopilot exception" in MISSION
   and "your approval is required only because it creates the delivery contract" in MISSION,
   "breakdown approval says why this human boundary exists")
ok("Lifecycle action <span class=\"badge bg-orange-lt ms-1\">Autopilot exception" in PROJECT_ADMIN,
   "destructive project lifecycle controls are labeled as exceptions")
ok("Provider credential enrollment and authorization to use paid capacity" in SETTINGS
   and "Once authorized, scheduling and execution remain automatic" in SETTINGS,
   "credential and paid-capacity authority is separated from routine Autopilot work")

print(f"\nUI-46 Autopilot exceptions: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
