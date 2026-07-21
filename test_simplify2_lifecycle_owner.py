#!/usr/bin/env python3
"""SIMPLIFY-2: one coordinator, one lifecycle stream, one session ensure verb."""
from __future__ import annotations

from pathlib import Path
import sys

import scripts.switchboard_path  # noqa: F401
from switchboard.application.commands import task_execution as execution


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def projection(*, role="implementation", active=True, pending=False, head="a" * 40):
    return {
        "active_runner": ({"runner_session_id": "runner-old", "host_id": "host/mac",
                           "metadata": {"role": role}} if active else None),
        "active_attempt": ({"wake_id": "wake-old", "status": "pending", "role": role}
                           if pending else None),
        "pr_head": {"head_sha": head, "pr_number": 42},
    }


saved_projection = execution._projection
saved_supersede = execution._supersede
saved_cancel = execution.coordination_repo.cancel_wake
saved_ticket = execution.runner_pty_command.mint_ticket_for_session
try:
    supersedes = []
    launches = []
    execution.runner_pty_command.mint_ticket_for_session = lambda **_kwargs: {}

    execution._projection = lambda *_a, **_k: projection(role="implementation")

    def supersede(*_args, **kwargs):
        supersedes.append(kwargs)
        return {"execution_id": "runner-old", "wake_id": ""}

    execution._supersede = supersede
    result = execution.start_task(
        "SEG-5", project="switchboard", role="reviewer",
        launcher=lambda *_a, **_k: launches.append(_k) or {"action": "started"},
    )
    ok(result.get("action") == "attach"
       and result.get("execution_id") == "runner-old"
       and result.get("role") is None
       and not supersedes and not launches,
       "a live agent attaches without privileged lifecycle-role replacement")

    execution._projection = lambda *_a, **_k: projection(
        role="implementation", active=False, pending=True, head="b" * 40)
    cancelled = []
    execution.coordination_repo.cancel_wake = (
        lambda wake_id, **kwargs: cancelled.append((wake_id, kwargs)) or {"cancelled": True}
    )
    result = execution.start_task(
        "SEG-5", project="switchboard", role="remediation",
        launcher=lambda *_a, **kwargs: launches.append(kwargs) or {
            "action": "started", "started": True, "wake_id": "wake-new"},
    )
    ok(not cancelled and result.get("action") == "starting"
       and result.get("wake_id") == "wake-old" and not launches,
       "an in-flight assignment is deduped without lifecycle-role replacement")
finally:
    execution._projection = saved_projection
    execution._supersede = saved_supersede
    execution.coordination_repo.cancel_wake = saved_cancel
    execution.runner_pty_command.mint_ticket_for_session = saved_ticket


root = Path(__file__).resolve().parent
mission = (root / "mission_coordinator.py").read_text()
review = (root / "review_steward.py").read_text()
remediation = (root / "src/switchboard/storage/repositories/review_remediations.py").read_text()
daemon = (root / "coordinator_daemon.py").read_text()
jobs = (root / "jobs.py").read_text()
redeploy = (root / "deploy/redeploy.sh").read_text()
agent_host = (root / "adapters/agent_host.py").read_text()

ok("request_wake(" not in mission + review + remediation
   and "claim_next(" not in mission + review + remediation,
   "ready, review, and remediation paths never assemble wakes or claim directly")
ok("auto_claim" not in mission and "auto_wake" not in mission
   and '"auto_start"' in mission,
   "assign, wake, and nudge policy knobs collapse to auto_start")
ok("task_execution.start_task" in mission + review
   and 'role="remediation"' in review
   and 'role="review_merge"' in review,
   "implementation, remediation, and review converge on start_task(role=...)")
ok("escalation_sender" not in review and "ACTION_ESCALATE" not in review,
   "routine review failures remain in the agent loop without an approval channel")
ok("PM_AUTOPILOT_COFLEET" not in mission + daemon + jobs + redeploy,
   "the legacy fleet pause flag is retired")
ok("coordinator_review" not in jobs and "coordinator_merge" not in jobs
   and not (root / "deploy/projectplanner-coordinator-review.timer").exists()
   and not (root / "deploy/projectplanner-coordinator-merge.timer").exists(),
   "review and merge have no independent job or timer stop-points")
ok(all(unit in redeploy for unit in (
    "projectplanner-coordinator-review.timer",
    "projectplanner-coordinator-review.service",
    "projectplanner-coordinator-merge.timer",
    "projectplanner-coordinator-merge.service",
)), "redeploy explicitly retires stale split lifecycle units")
aux_units = redeploy.split("AUX_UNITS=(", 1)[1].split(")", 1)[0]
ok("projectplanner-coordinator-autopilot.service" in aux_units
   and 'systemctl is-active --quiet "$u"' in redeploy
   and 'systemctl restart "$u"' in redeploy,
   "redeploy restarts the active unified lifecycle owner after updating its code")
ok('"decision_stream"' in daemon
   and "review_steward.steward_project" in daemon
   and "merge_steward.steward_project" in daemon,
   "one leader tick emits the ordered review, merge, reconcile, and execution stream")
ok('"lifecycle_role"' in agent_host and 'assignment.get("role")' in agent_host,
   "runner registration preserves the ensured lifecycle role")

print(f"\nSIMPLIFY-2 lifecycle owner: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
