#!/usr/bin/env python3
"""COORD-6 — exception-only human escalation channel."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="coord6-escalation-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
# Force notify dry-run (no Slack/SMTP).
for key in ("PM_SLACK_WEBHOOK_URL", "PM_SMTP_HOST", "PM_NOTIFY_EMAIL_TO"):
    os.environ.pop(key, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import coordinator_escalation as esc  # noqa: E402
import mission_coordinator  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    store.init_db("switchboard")

    # --- pure filter: agent-lane actions never page humans ---
    ok(not esc.should_notify_human(action="claim_task"),
       "claim_task stays agent-to-agent")
    ok(not esc.should_notify_human(action="verify_merge_provenance", automatic=True),
       "verify_merge_provenance stays agent-to-agent")
    ok(esc.should_notify_human(action="request_human_approval", attention=True),
       "request_human_approval pages humans")
    ok(esc.should_notify_human(escalation_class="no_eligible_host"),
       "no_eligible_host alias maps to human escalation")
    ok(esc.should_notify_human(escalation_class="budget_breach"),
       "budget_breach pages humans")
    ok(esc.should_notify_human(escalation_class="security/secrets boundary"),
       "security/secrets boundary pages humans")

    plan = esc.build_escalation_plan(
        escalation_class="human_gate_required",
        project="switchboard",
        task_id="COORD-6",
        deliverable_id="demo",
        failed_condition="Human gate blocked: spend approval",
    )
    ok(plan and plan["schema"] == esc.SCHEMA, "build_escalation_plan returns v1 schema")
    ok(plan["task_id"] == "COORD-6"
       and "Human gate" in plan["failed_condition"]
       and plan["recommended_choices"]
       and "Approve or reject" in plan["minimum_decision"],
       "plan includes task, failed condition, choices, minimum decision")

    body = esc.format_human_notification(plan)
    ok("Task: COORD-6" in body
       and "Failed condition:" in body
       and "Minimum decision needed:" in body
       and "Recommended choices:" in body
       and "[approve]" in body,
       "notification body is structured for operators")

    # Agent-lane action must not classify.
    ok(esc.classify_mission_action(
        {"action": "claim_task", "task_id": "X", "automatic": True},
        project="switchboard") is None,
       "classify_mission_action skips claim_task")

    gate_plan = esc.classify_mission_action(
        {"action": "request_human_approval", "task_id": "RENDER-1",
         "reason": "Needs sign-off", "attention": True, "delivery_impact": "blocking"},
        project="switchboard", deliverable_id="coord-mission",
    )
    ok(gate_plan and gate_plan["escalation_class"] == "human_gate_required",
       "mission human gate classifies as human_gate_required")

    # --- delivery + dedupe against a real board DB ---
    first = esc.deliver_human_escalation(
        plan, store_mod=store, actor="agent/coordinator",
        alert_to="switchboard/operator", notify_outbound=True, now=1_700_000_000.0)
    ok(first.get("ok") and first.get("delivered") and first.get("message_id"),
       "first delivery sends operator message")
    ok(any(n.get("dry_run") for n in (first.get("notify") or [])),
       "outbound notify is dry-run without Slack/SMTP creds")

    inbox = store.list_unacked_messages(
        to_agent="switchboard/operator", project="switchboard")
    ok(any(m.get("signal") == esc.SIGNAL for m in inbox),
       "operator inbox receives coordinator_escalation signal")
    msg = next(m for m in inbox if m.get("signal") == esc.SIGNAL)
    ok("Task: COORD-6" in (msg.get("message") or "")
       and "Minimum decision needed:" in (msg.get("message") or ""),
       "inbox message carries structured escalation body")

    second = esc.deliver_human_escalation(
        plan, store_mod=store, actor="agent/coordinator",
        alert_to="switchboard/operator", notify_outbound=True, now=1_700_000_000.0)
    ok(second.get("deduped") and not second.get("delivered"),
       "same exception in the same dedupe window is not re-sent")

    later = esc.deliver_human_escalation(
        plan, store_mod=store, actor="agent/coordinator",
        alert_to="switchboard/operator", notify_outbound=True,
        now=1_700_000_000.0 + esc.DEFAULT_DEDUPE_WINDOW_S * 2)
    ok(later.get("deduped") and not later.get("delivered"),
       "unchanged escalation signature is not re-sent in a later time window")

    audit = store.audit_export(project="switchboard")
    ok(any(a.get("kind") == esc.ACTIVITY_KIND for a in audit.get("activity") or []),
       "activity log records coordinator.escalation")

    # Dispatch blocked without host → escalate; generic claim miss → do not.
    no_host = esc.classify_dispatch_blocked(
        {"requested": False, "eligible_host_count": 0,
         "reason": "No eligible host in switchboard"},
        project="switchboard", task_id="COORD-4",
    )
    ok(no_host and no_host["escalation_class"] == "unreachable_agent_no_host",
       "no eligible host escalates")
    ok(esc.classify_dispatch_blocked(
        {"claimed": False, "reason": "no ready unblocked tasks"},
        project="switchboard") is None,
       "generic claim miss does not page humans")

    # --- wired into mission coordinator tick ---
    home = store.create_project("Esc Home", project_id="qa-esc-home", actor="test")
    target = store.create_project("Esc Target", project_id="qa-esc-target", actor="test")
    ok(home.get("created") and target.get("created"), "tick test projects created")
    store.create_task(
        {"workstream_id": "RENDER", "title": "Gated linked task"},
        actor="test", project="qa-esc-target",
    )
    mission = store.create_project_board(
        {"id": "esc-mission", "title": "Esc Mission", "kind": "mission", "status": "active"},
        actor="test", project="qa-esc-home",
    )
    store.create_deliverable(
        {"id": "esc-mission", "board_id": mission["id"], "title": "Esc Mission",
         "status": "approved", "end_state": "Escalation works."},
        actor="test", project="qa-esc-home",
    )
    ms = store.add_deliverable_milestone(
        "esc-mission", {"title": "Gate", "status": "in_progress"},
        actor="test", project="qa-esc-home",
    )
    store.link_task_to_deliverable(
        "esc-mission", "qa-esc-target", "RENDER-1",
        milestone_id=ms["milestones"][0]["id"],
        data={"role": "contributes", "blocks_deliverable": True},
        actor="test", project="qa-esc-home",
    )
    store.set_agent_state("RENDER-1", "human_gate", {
        "required": True,
        "approval_reason": "Needs product sign-off",
    }, project="qa-esc-target")

    tick = store.run_mission_coordinator_tick(
        project="qa-esc-home",
        deliverable_id="esc-mission",
        coordinator_agent_id="agent/coordinator",
        actor="test",
        policy={"auto_claim": True, "worker_agent_id": "agent/worker",
                "auto_refresh_brief": False},
        idem_key="esc-tick-1",
    )
    ok(tick.get("status") == "human_required", "tick status is human_required")
    notes = tick.get("human_notifications") or []
    ok(notes and notes[0].get("delivered") and notes[0].get("message_id"),
       "tick delivers human escalation notification")
    ok((notes[0].get("plan") or {}).get("escalation_class") == "human_gate_required",
       "tick notification carries escalation_class")
    ok((notes[0].get("plan") or {}).get("recommended_choices"),
       "tick notification includes recommended choices")
    ok(tick.get("decision", {}).get("decision_kind") == "human_escalation",
       "tick still records COORD-3 human_escalation decision")

    # Claim path (agent-to-agent) must not produce human notifications.
    store.set_agent_state("RENDER-1", "human_gate", {
        "required": True,
        "approved": True,
        "approved_by": "test",
        "approval_reason": "Signed off",
    }, project="qa-esc-target")
    store.update_task("RENDER-1", {"status": "Not Started", "assignee": None},
                      actor="test", project="qa-esc-target")
    claim_tick = store.run_mission_coordinator_tick(
        project="qa-esc-home",
        deliverable_id="esc-mission",
        coordinator_agent_id="agent/coordinator",
        actor="test",
        policy={"auto_claim": True, "worker_agent_id": "agent/worker",
                "auto_refresh_brief": False},
        idem_key="esc-tick-claim",
    )
    ok(claim_tick.get("status") == "claimed", "after approval, tick claims via agents")
    ok(not (claim_tick.get("human_notifications") or []),
       "successful claim path does not page humans")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
