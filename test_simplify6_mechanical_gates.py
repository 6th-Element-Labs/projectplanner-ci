#!/usr/bin/env python3
"""SIMPLIFY-6: routine progress is mechanical; humans are exception-only."""
from __future__ import annotations

from pathlib import Path
import sys

import coordinator_escalation as escalation
import merge_steward


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


legacy = merge_steward.load_merge_policy(
    env={
        "PM_COORDINATOR_MERGE_ENABLED": "0",
        "PM_COORDINATOR_MERGE_AUTHORITY": "0",
        "PM_COORDINATOR_MERGE_RISK_CEILING": "Low",
    },
    meta={
        "enabled": False,
        "authority_granted": False,
        "deny_blocking_tasks": True,
        "risk_ceiling": "Low",
    },
)
subjective_policy = {
    "enabled", "authority_granted", "deny_blocking_tasks", "risk_ceiling",
}
ok(not subjective_policy.intersection(legacy),
   "legacy approval, blocking-task, and risk-ceiling switches are ignored")

human_classes = {
    name for name in escalation.ESCALATION_CLASSES
    if escalation.should_notify_human(escalation_class=name)
}
ok(human_classes == {
    "ambiguous_requirements", "budget_breach", "repeated_failures",
    "security_secrets_boundary",
}, "only irreducible decisions may notify a human")

routine_classes = {
    "failed_gate", "stale_branch_conflict", "missing_provenance",
    "absent_permission", "unreachable_agent_no_host", "unbound_identity",
    "policy_violation", "red_ci_product_judgment",
}
ok(not any(escalation.should_notify_human(escalation_class=name)
           for name in routine_classes),
   "CI, conflicts, provenance, permissions, capacity, identity, and policy labels never page")

root = Path(__file__).resolve().parent
merge_source = (root / "merge_steward.py").read_text()
review_source = (root / "review_steward.py").read_text()
app_source = (root / "static/app.js").read_text()
mission_source = (root / "static/js/mission.js").read_text()

ok("ACTION_ESCALATE" not in merge_source + review_source
   and "hold_mechanical_gate" in merge_source + review_source,
   "review and merge record mechanical holds instead of approval escalations")
ok("no human approval is required" in review_source,
   "review_merge instructions continue through green mechanical gates")
ok("Human-gated.</span> This task is blocking" not in app_source,
   "blocking work no longer displays a false maintainer-approval warning")
ok("['external_ci', 'publication_evidence', 'human_gate']" not in mission_source,
   "legacy human-gate metadata is absent from mission policy-drift blockers")

print(f"\nSIMPLIFY-6 mechanical gates: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
