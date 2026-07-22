#!/usr/bin/env python3
"""BUG-112: parked links and skipped milestones never enter automatic dispatch."""
import os
import shutil
import sys
import tempfile

from path_setup import ROOT

_TMP = tempfile.mkdtemp(prefix="bug112-parked-dispatch-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP

assert ROOT.is_dir()

import coordinator_daemon  # noqa: E402
import mission_coordinator  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_project_registry()
    store.init_db("switchboard")
    store.create_project("BUG-112 Home", project_id="qa-bug112-home", actor="test")
    store.create_project("BUG-112 Tasks", project_id="qa-bug112-tasks", actor="test")

    store.create_project_board(
        {"id": "bug112-mission", "title": "BUG-112 Mission", "kind": "mission",
         "status": "active"},
        actor="test", project="qa-bug112-home")
    store.create_deliverable(
        {"id": "bug112-deliverable", "board_id": "bug112-mission",
         "title": "BUG-112 Deliverable", "status": "approved"},
        actor="test", project="qa-bug112-home")
    active = store.add_deliverable_milestone(
        "bug112-deliverable", {"id": "active", "title": "Active", "status": "in_progress"},
        actor="test", project="qa-bug112-home")
    skipped = store.add_deliverable_milestone(
        "bug112-deliverable", {"id": "skipped", "title": "Skipped", "status": "skipped"},
        actor="test", project="qa-bug112-home")
    active_id = next(row["id"] for row in active["milestones"] if row["title"] == "Active")
    skipped_id = next(row["id"] for row in skipped["milestones"] if row["title"] == "Skipped")

    for title in (
        "Parked first", "Explicitly false", "Skipped milestone", "Active flow",
    ):
        store.create_task(
            {"workstream_id": "FLOW", "title": title},
            actor="test", project="qa-bug112-tasks")

    links = (
        ("FLOW-1", active_id, {"role": "parked", "blocks_deliverable": False,
                                "metadata": {"dispatch_eligible": True}}),
        ("FLOW-2", active_id, {"role": "contributes", "blocks_deliverable": True,
                                "metadata": {"dispatch_eligible": False}}),
        ("FLOW-3", skipped_id, {"role": "contributes", "blocks_deliverable": True}),
        ("FLOW-4", active_id, {"role": "contributes", "blocks_deliverable": True}),
    )
    for task_id, milestone_id, data in links:
        store.link_task_to_deliverable(
            "bug112-deliverable", "qa-bug112-tasks", task_id,
            milestone_id=milestone_id, data=data, actor="test",
            project="qa-bug112-home")

    status = store.get_mission_status(
        project="qa-bug112-home", deliverable_id="bug112-deliverable")
    automatic = [
        action.get("task_id") for action in status.get("next_actions") or []
        if action.get("action") in {"claim_task", "resume_or_claim"}
    ]
    scope = {row["task_id"]: row for row in status["dispatch_scope"]["links"]}
    ok(automatic == ["FLOW-2", "FLOW-4"],
       "Autopilot exposes active flow links regardless of legacy metadata")
    ok(scope["FLOW-1"]["reason"] == "context_role:parked"
       and scope["FLOW-2"]["reason"] == "automatic_flow"
       and scope["FLOW-3"]["reason"] == "milestone_skipped",
       "mission dispatch scope derives eligibility from structural state")
    linked = {row["task_id"]: row for row in status["linked_tasks"]}
    ok("dispatch_eligible" not in linked["FLOW-1"]["metadata"]
       and "dispatch_eligible" not in linked["FLOW-2"]["metadata"],
       "legacy dispatch flags are stripped from stored link projections")
    with store._conn("qa-bug112-home") as c:
        raw_metadata = [row[0] for row in c.execute(
            "SELECT metadata_json FROM deliverable_task_links ORDER BY task_id").fetchall()]
    ok(all("dispatch_eligible" not in value for value in raw_metadata),
       "legacy dispatch flags are stripped before link metadata is stored")
    explicit_skipped = mission_coordinator.coordinator_tick_plan(
        status, policy={"target_task_id": "FLOW-3"})
    ok(explicit_skipped.get("status") == "idle",
       "explicit targeting cannot bypass a skipped milestone")
    daemon = coordinator_daemon.CoordinatorDaemon(
        coordinator_daemon.DaemonConfig(projects=("qa-bug112-home",), act=False),
        store_mod=store, instance_id="bug112-test")
    parked_candidates = daemon._scope_candidates(
        {"scope_type": "task", "task_project": "qa-bug112-tasks", "task_id": "FLOW-1"},
        status)
    skipped_candidates = daemon._scope_candidates(
        {"scope_type": "task", "task_project": "qa-bug112-tasks", "task_id": "FLOW-3"},
        status)
    ok(not parked_candidates and not skipped_candidates,
       "task-scoped Autopilot cannot bypass parked or skipped link policy")
    parked_start = store.start_autopilot_scope(
        project="qa-bug112-home", deliverable_id="bug112-deliverable",
        scope_type="task", task_project="qa-bug112-tasks", task_id="FLOW-1")
    ok(parked_start.get("blocker", {}).get("reason") == "context_role:parked",
       "explicit Start returns the structural blocker without creating a scope")

    claimed = store.claim_next(
        "agent/bug112", deliverable_id="bug112-deliverable",
        project="qa-bug112-home")
    reason = claimed.get("dispatch_reason") or {}
    ok(claimed.get("claimed") and claimed["task"]["task_id"] == "FLOW-2",
       "legacy false cannot veto an eligible active flow task")
    ok(reason.get("candidate_count") == 2
       and reason.get("skipped", {}).get("link_policy") == 1
       and reason.get("skipped", {}).get("skipped_milestone") == 1,
       "claim_next reports only structural link and milestone exclusions")
    findings = reason.get("link_policy_findings") or {}
    ok(findings["qa-bug112-tasks:FLOW-1"]["reason"] == "context_role:parked"
       and findings["qa-bug112-tasks:FLOW-3"]["reason"] == "milestone_skipped",
       "claim receipt preserves exact link-policy exclusion reasons")

    second = store.claim_next(
        "agent/bug112-repeat", deliverable_id="bug112-deliverable",
        project="qa-bug112-home")
    ok(second.get("claimed") and second["task"]["task_id"] == "FLOW-4",
       "repeated claim_next selects the remaining eligible flow task")
    ok(store.get_task("FLOW-1", project="qa-bug112-tasks")["status"] == "Not Started"
       and store.get_task("FLOW-3", project="qa-bug112-tasks")["status"] == "Not Started",
       "excluded tasks remain untouched")

    store.link_task_to_deliverable(
        "bug112-deliverable", "qa-bug112-tasks", "FLOW-1",
        milestone_id=active_id,
        data={"role": "contributes", "blocks_deliverable": False},
        actor="operator/reactivate", project="qa-bug112-home")
    reactivated = store.claim_next(
        "agent/bug112-reactivated", deliverable_id="bug112-deliverable",
        project="qa-bug112-home")
    ok(reactivated.get("claimed") and reactivated["task"]["task_id"] == "FLOW-1",
       "an operator can explicitly reactivate a link into automatic scope")
    reactivated_status = store.get_mission_status(
        project="qa-bug112-home", deliverable_id="bug112-deliverable")
    reactivated_candidates = daemon._scope_candidates(
        {"scope_type": "task", "task_project": "qa-bug112-tasks", "task_id": "FLOW-1"},
        reactivated_status)
    ok(reactivated_candidates and reactivated_candidates[0]["task_id"] == "FLOW-1",
       "task-scoped Autopilot sees the explicitly reactivated link")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
