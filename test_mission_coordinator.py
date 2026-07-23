#!/usr/bin/env python3
"""Tests for deliverable-scoped mission coordinator loop (DELIVERABLES-7)."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mission-coordinator-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mission_coordinator  # noqa: E402
import store  # noqa: E402
from switchboard.application.commands import task_execution  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    starts = []

    def fake_start_task(task_id, **kwargs):
        starts.append({"task_id": task_id, **kwargs})
        return {"action": "started", "started": True,
                "wake_id": f"wake-{len(starts)}", "role": kwargs.get("role")}

    task_execution.start_task = fake_start_task
    store.init_project_registry()
    store.init_db("switchboard")
    home = store.create_project("Coord Home", project_id="qa-coord-home", actor="test")
    target = store.create_project("Coord Target", project_id="qa-coord-target", actor="test")
    ok(home.get("created") and target.get("created"), "test projects created")

    store.create_task(
        {"workstream_id": "RENDER", "title": "Ready linked task"},
        actor="test", project="qa-coord-target",
    )
    store.create_task(
        {"workstream_id": "RENDER", "title": "Stray task", "sort_order": 999},
        actor="test", project="qa-coord-target",
    )
    mission = store.create_project_board(
        {"id": "coord-mission", "title": "Coord Mission", "kind": "mission", "status": "active"},
        actor="test", project="qa-coord-home",
    )
    store.create_deliverable(
        {"id": "coord-mission", "board_id": mission["id"], "title": "Coord Mission",
         "status": "approved", "end_state": "Cross-board ingest works."},
        actor="test", project="qa-coord-home",
    )
    ms = store.add_deliverable_milestone(
        "coord-mission", {"title": "Build ingest", "status": "in_progress"},
        actor="test", project="qa-coord-home",
    )
    milestone_id = ms["milestones"][0]["id"]
    store.link_task_to_deliverable(
        "coord-mission", "qa-coord-target", "RENDER-1", milestone_id=milestone_id,
        data={"role": "contributes", "blocks_deliverable": True},
        actor="test", project="qa-coord-home",
    )
    store.register_agent(
        "agent/coordinator", "codex", ttl_s=1000,
        actor="test", project="qa-coord-home")
    scope = store.start_autopilot_scope(
        project="qa-coord-home", deliverable_id="coord-mission",
        actor="test")
    scope_authority = store.acquire_autopilot_scope_lease(
        scope["scope_id"], holder_agent_id="agent/coordinator",
        project="qa-coord-home")

    # BUG-143 mixed scope: ready flow work is selected structurally. Parked
    # context stays visible and dependencies still block dispatch.
    store.create_task(
        {"workstream_id": "VENDOR", "title": "Vendor gate", "status": "Blocked"},
        actor="test", project="qa-coord-target",
    )
    for title in ("Ready parked task", "Ready nonblocking task", "Vendor follow-on"):
        payload = {"workstream_id": "RENDER", "title": title}
        if title == "Vendor follow-on":
            payload["depends_on"] = ["VENDOR-1"]
        store.create_task(payload, actor="test", project="qa-coord-target")
    store.link_task_to_deliverable(
        "coord-mission", "qa-coord-target", "RENDER-3", milestone_id=milestone_id,
        data={"role": "parked", "blocks_deliverable": False},
        actor="test", project="qa-coord-home")
    store.link_task_to_deliverable(
        "coord-mission", "qa-coord-target", "RENDER-4", milestone_id=milestone_id,
        data={"role": "contributes", "blocks_deliverable": False},
        actor="test", project="qa-coord-home")
    store.link_task_to_deliverable(
        "coord-mission", "qa-coord-target", "RENDER-5", milestone_id=milestone_id,
        data={"role": "contributes", "blocks_deliverable": True},
        actor="test", project="qa-coord-home")

    status = store.get_mission_status(project="qa-coord-home", deliverable_id="coord-mission")
    generic_claims = [a.get("task_id") for a in status.get("next_actions") or []
                      if a.get("action") in {"claim_task", "resume_or_claim"}]
    ok(generic_claims == ["RENDER-1", "RENDER-4"],
       "mixed deliverable selects ready flow work regardless of delivery blocking")
    scope_rows = {row.get("task_id"): row for row in status["dispatch_scope"]["links"]}
    ok(scope_rows["RENDER-3"]["reason"] == "context_role:parked"
       and scope_rows["RENDER-4"]["reason"] == "automatic_flow",
       "parked context is excluded while nonblocking flow remains eligible")
    ok(not any(b.get("task_id") in {"RENDER-3", "RENDER-4"}
               for b in status.get("blockers") or []),
       "parked and nonblocking context links do not become delivery blockers")
    explicit = mission_coordinator.coordinator_tick_plan(
        status, policy={"target_task_id": "RENDER-4"})
    parked = mission_coordinator.coordinator_tick_plan(
        status, policy={"target_task_id": "RENDER-3"})
    ok(explicit.get("status") == "dispatch_ready"
       and explicit.get("dispatch", {}).get("task_id") == "RENDER-4",
       "explicit task policy may opt a nonblocking flow task into dispatch")
    ok(parked.get("status") == "idle",
       "explicit targeting still cannot auto-dispatch a parked link")
    duplicate_id_status = {
        "deliverable_id": "cross-project-duplicate", "progress": {},
        "linked_tasks": [
            {"project_id": "project-a", "task_id": "SAME-1", "role": "contributes",
             "task_detail": {"task_id": "SAME-1", "status": "Not Started",
                             "dependency_state": {"ready": True}, "active_claims": [],
                             "workstream": "RENDER"}},
            {"project_id": "project-b", "task_id": "SAME-1", "role": "contributes",
             "task_detail": {"task_id": "SAME-1", "status": "Not Started",
                             "dependency_state": {"ready": True}, "active_claims": [],
                             "workstream": "RENDER"}},
        ],
    }
    project_target = mission_coordinator.coordinator_tick_plan(
        duplicate_id_status,
        policy={"target_task_id": "SAME-1", "target_project_id": "project-b"})
    ok(project_target.get("dispatch", {}).get("project_id") == "project-b",
       "explicit task targeting is fenced by project when task ids overlap")
    lane_fail_closed = mission_coordinator.coordinator_tick_plan(
        {"deliverable_id": "coord-mission", "progress": {}, "next_actions": [
            {"action": "claim_task", "task_id": "NO-LANE"},
        ]},
        policy={"allowed_lanes": ["RENDER"]},
    )
    ok(lane_fail_closed.get("status") == "idle",
       "lane allowlist fails closed when an action has no lane metadata")
    plan = mission_coordinator.coordinator_tick_plan(
        status, policy={"auto_start": True})
    ok(plan.get("status") == "dispatch_ready" and
       plan.get("dispatch", {}).get("action") == "claim_task",
       "coordinator plan selects claim_task for ready linked work")
    skipped = mission_coordinator._skipped_alternatives(
        {"next_actions": [
            {"action": "claim_task", "task_id": "RENDER-2", "reason": "ready"},
            {"action": "claim_task", "task_id": "RENDER-1", "reason": "ready"},
            {"action": "resume_or_claim", "task_id": "RENDER-3"},
        ]},
        {"action": "claim_task", "task_id": "RENDER-1"},
        plan_status="dispatch_ready",
    )
    ok({item.get("reason") for item in skipped} ==
       {"task_id_tiebreak", "lower_action_priority"},
       "skipped alternatives explain the planner priority and task-id tiebreak")

    tick = store.run_mission_coordinator_tick(
        project="qa-coord-home",
        deliverable_id="coord-mission",
        coordinator_agent_id="agent/coordinator",
        actor="test",
        policy={"auto_start": True, "auto_refresh_brief": True},
        scope_authority=scope_authority,
        idem_key="tick-1",
    )
    ok(tick.get("schema") == "switchboard.mission_coordinator_tick.v1",
       "coordinator tick returns v1 schema")
    ok(tick.get("status") == "session_ensured"
       and tick.get("dispatch", {}).get("started"),
       "coordinator tick ensures the ready linked task session")
    ok(tick.get("dispatch", {}).get("task_project") == "qa-coord-target",
       "coordinator dispatch records cross-project task_project")
    ok(tick.get("deliverable_id") == "coord-mission",
       "coordinator tick is scoped to deliverable_id")
    ok(isinstance(tick.get("decision"), dict)
       and str(tick.get("decision_id") or tick["decision"].get("decision_id", "")).startswith(
           "coorddec-"),
       "coordinator tick records explainable decision_id")
    expected_tick_id = store.coordinator_decision_id(
        project="qa-coord-home", task_id="RENDER-1",
        deliverable_id="coord-mission", coordinator_agent_id="agent/coordinator",
        decision_kind="action", inputs_snapshot={}, policy_rule="",
        chosen_action={}, stable_key="tick-1")
    ok(tick.get("decision_id") == expected_tick_id,
       "caller idem_key is the durable coordinator decision identity")
    ok(tick["decision"].get("policy_rule")
       and tick["decision"].get("chosen_action")
       and "skipped_alternatives" in tick["decision"]
       and tick["decision"].get("inputs_snapshot"),
       "decision has inputs, policy, chosen action, skipped alternatives")
    trail = store.list_coordinator_decisions(
        deliverable_id="coord-mission", project="qa-coord-home")
    ok(any(d.get("decision_id") == tick.get("decision_id") for d in trail),
       "decision trail is listable without chat transcripts")

    audit = store.audit_export(project="qa-coord-home")
    ok(any(a.get("kind") == "deliverable.coordinator_tick"
           for a in audit.get("activity") or []),
       "coordinator tick is audited on mission-home project")

    store.update_task("RENDER-1", {"status": "Not Started", "assignee": None},
                      actor="test", project="qa-coord-target")

    wake_tick = store.run_mission_coordinator_tick(
        project="qa-coord-home",
        deliverable_id="coord-mission",
        coordinator_agent_id="agent/coordinator",
        actor="test",
        policy={"auto_start": True},
        scope_authority=scope_authority,
        idem_key="tick-wake",
    )
    wake_decision = wake_tick.get("decision") or {}
    ok(wake_tick.get("status") == "session_ensured" and
       wake_decision.get("decision_kind") == "action" and
       wake_decision.get("result", {}).get("dispatch", {}).get("wake_id"),
       "start_task persists the selected task and observed ensure result")

    store.set_agent_state("RENDER-1", "human_gate", {
        "required": True,
        "approval_reason": "Needs sign-off",
    }, project="qa-coord-target")
    gated = store.get_mission_status(project="qa-coord-home", deliverable_id="coord-mission")
    gated_plan = mission_coordinator.coordinator_tick_plan(gated)
    ok(gated_plan.get("status") == "dispatch_ready",
       "legacy human-gate metadata remains dispatchable")

    gated_tick = store.run_mission_coordinator_tick(
        project="qa-coord-home",
        deliverable_id="coord-mission",
        coordinator_agent_id="agent/coordinator",
        actor="test",
        policy={"auto_start": True},
        scope_authority=scope_authority,
        idem_key="tick-gated",
    )
    ok(gated_tick.get("status") == "session_ensured" and not gated_tick.get("escalations"),
       "coordinator ensures a session despite legacy human-gate metadata")
    ok(gated_tick.get("decision", {}).get("decision_kind") != "human_escalation",
       "coordinator records no human escalation")

    store.set_agent_state("RENDER-1", "human_gate", {
        "required": True,
        "approved": True,
        "approved_by": "test",
        "approval_reason": "Signed off",
    }, project="qa-coord-target")
    store.mark_task_pr_opened(
        "RENDER-1", 42, pr_url="https://github.com/example/repo/pull/42",
        branch="codex/render-1", head_sha="a" * 40,
        actor="test", project="qa-coord-target")
    review_status = store.get_mission_status(
        project="qa-coord-home", deliverable_id="coord-mission")
    review_plan = mission_coordinator.coordinator_tick_plan(review_status)
    ok(review_plan.get("status") == "monitor" and
       review_plan.get("monitors", [{}])[0].get("action") == "verify_merge_provenance",
       "In Review tasks monitor merge provenance instead of claiming Done")
    review_tick = store.run_mission_coordinator_tick(
        project="qa-coord-home",
        deliverable_id="coord-mission",
        coordinator_agent_id="agent/coordinator",
        actor="test",
        policy={"auto_start": True, "auto_refresh_brief": False},
        scope_authority=scope_authority,
        idem_key="tick-review",
    )
    review_decision = review_tick.get("decision") or {}
    ok(review_tick.get("status") == "session_ensured" and
       review_tick.get("dispatch", {}).get("role") == "review_merge" and
       review_tick.get("dispatch", {}).get("head_sha") == "a" * 40 and
       review_decision.get("result", {}).get("monitors"),
       "In Review ensures one exact-head review_merge owner")

    idem_repeat = store.run_mission_coordinator_tick(
        project="qa-coord-home",
        deliverable_id="coord-mission",
        coordinator_agent_id="agent/coordinator",
        actor="test",
        policy={"auto_start": True, "auto_refresh_brief": True},
        scope_authority=scope_authority,
        idem_key="tick-1",
    )
    ok(idem_repeat.get("status") == "session_ensured"
       and idem_repeat.get("dispatch", {}).get("started"),
       "coordinator tick is idempotent by idem_key")
    ok(idem_repeat.get("decision_id") == tick.get("decision_id"),
       "idempotent tick returns the same durable decision id")
    complete_trail = store.list_coordinator_decisions(
        deliverable_id="coord-mission", project="qa-coord-home")
    ok(len(complete_trail) == 4 and
       {item.get("decision_kind") for item in complete_trail} == {"action"},
       "decision trail stores completion-owner session ensures without replay duplicates")
    wrong_holder = {
        **scope_authority, "holder_agent_id": "agent/not-the-owner"}
    denied = store.validate_autopilot_scope_authority(
        wrong_holder, project="qa-coord-home",
        deliverable_id="coord-mission")
    ok(not denied.get("allowed")
       and "holder_agent_id" in denied.get("reason_codes", []),
       "scope effects fail closed for the wrong holder")
    store.register_agent(
        "agent/replacement", "codex", ttl_s=1000,
        actor="test", project="qa-coord-home")
    replacement = store.acquire_autopilot_scope_lease(
        scope["scope_id"], holder_agent_id="agent/replacement",
        project="qa-coord-home", now=float(scope_authority["expires_at"]) + 1)
    stale = store.validate_autopilot_scope_authority(
        scope_authority, project="qa-coord-home",
        deliverable_id="coord-mission",
        now=float(scope_authority["expires_at"]) + 1)
    current = store.validate_autopilot_scope_authority(
        replacement, project="qa-coord-home",
        deliverable_id="coord-mission",
        now=float(scope_authority["expires_at"]) + 1)
    ok(replacement.get("takeover") and not stale.get("allowed")
       and current.get("allowed")
       and replacement.get("fence_epoch") > scope_authority.get("fence_epoch"),
       "expired scope takeover advances the fence and invalidates the old owner")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
