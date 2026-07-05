#!/usr/bin/env python3
"""Tests for deliverable-scoped claim_next and complete_claim."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="deliverables-claim-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
    home = store.create_project("Claim Home", project_id="qa-claim-home", actor="test")
    target = store.create_project("Claim Target", project_id="qa-claim-target", actor="test")
    ok(home.get("created") and target.get("created"), "test projects created")

    linked_task = store.create_task(
        {"workstream_id": "RENDER", "title": "Linked ingest path"},
        actor="test",
        project="qa-claim-target",
    )
    stray_task = store.create_task(
        {"workstream_id": "RENDER", "title": "Stray ready task", "sort_order": 9999},
        actor="test",
        project="qa-claim-target",
    )
    ok(linked_task["task_id"] == "RENDER-1" and stray_task["task_id"] == "RENDER-2",
       "target tasks created")

    mission = store.create_project_board(
        {
            "id": "claim-mission",
            "title": "Claim Mission",
            "kind": "mission",
            "status": "active",
        },
        actor="test",
        project="qa-claim-home",
    )
    deliverable = store.create_deliverable(
        {
            "id": "claim-mission",
            "board_id": mission["id"],
            "title": "Claim Mission",
            "status": "approved",
        },
        actor="test",
        project="qa-claim-home",
    )
    with_milestone = store.add_deliverable_milestone(
        "claim-mission",
        {"title": "Build ingest", "status": "in_progress"},
        actor="test",
        project="qa-claim-home",
    )
    milestone_id = with_milestone["milestones"][0]["id"]
    other_milestone = store.add_deliverable_milestone(
        "claim-mission",
        {"title": "Prove parity", "status": "not_started"},
        actor="test",
        project="qa-claim-home",
    )
    other_milestone_id = other_milestone["milestones"][1]["id"]

    store.link_task_to_deliverable(
        "claim-mission",
        "qa-claim-target",
        "RENDER-1",
        milestone_id=milestone_id,
        actor="test",
        project="qa-claim-home",
    )
    store.link_task_to_deliverable(
        "claim-mission",
        "qa-claim-target",
        "RENDER-2",
        milestone_id=other_milestone_id,
        actor="test",
        project="qa-claim-home",
    )

    unscoped = store.claim_next("agent/unscoped", project="qa-claim-target")
    ok(unscoped.get("claimed") and unscoped["task"]["task_id"] == "RENDER-1",
       "unscoped claim_next still picks highest-priority ready task on target board")

    store.abandon_claim(unscoped["claim_id"], "test reset", project="qa-claim-target")
    store.update_task("RENDER-1", {"status": "Not Started", "assignee": None},
                      actor="test", project="qa-claim-target")

    mission_claim = store.claim_next(
        "agent/mission",
        deliverable_id="claim-mission",
        project="qa-claim-home",
    )
    ok(mission_claim.get("claimed") and mission_claim["task"]["task_id"] == "RENDER-1",
       "deliverable-scoped claim_next claims linked task on target project")
    ok(mission_claim.get("task_project") == "qa-claim-target",
       "mission claim records task_project for cross-board completion")
    ok(mission_claim.get("dispatch_reason", {}).get("policy") == "mission_scope.v1",
       "mission claim dispatch_reason uses mission scope policy")

    store.abandon_claim(mission_claim["claim_id"], "test reset", project="qa-claim-target")
    store.update_task("RENDER-1", {"status": "Not Started", "assignee": None},
                      actor="test", project="qa-claim-target")
    store.update_task("RENDER-2", {"status": "Not Started", "assignee": None},
                      actor="test", project="qa-claim-target")

    milestone_claim = store.claim_next(
        "agent/milestone",
        deliverable_id="claim-mission",
        milestone_id=milestone_id,
        project="qa-claim-home",
    )
    ok(milestone_claim.get("claimed") and milestone_claim["task"]["task_id"] == "RENDER-1",
       "milestone filter claims only tasks linked to that milestone")
    ok(milestone_claim.get("milestone_id") == milestone_id,
       "milestone claim echoes milestone_id")

    store.abandon_claim(milestone_claim["claim_id"], "test reset", project="qa-claim-target")
    store.update_task("RENDER-1", {"status": "Not Started", "assignee": None},
                      actor="test", project="qa-claim-target")

    wrong_milestone = store.claim_next(
        "agent/wrong-milestone",
        deliverable_id="claim-mission",
        milestone_id="claim-mission:missing-milestone",
        project="qa-claim-home",
    )
    ok(not wrong_milestone.get("claimed") and
       wrong_milestone.get("reason") == "no_milestone_tasks",
       "unknown milestone returns no_milestone_tasks without wandering")

    finish_claim = store.claim_next(
        "agent/finish",
        deliverable_id="claim-mission",
        project="qa-claim-home",
    )
    ok(finish_claim.get("claimed"), "reclaim linked task for completion test")

    completed = store.complete_claim(
        finish_claim["claim_id"],
        evidence=json.dumps({
            "branch": "cursor/test",
            "head_sha": "abc123",
            "deliverable_id": "claim-mission",
            "mission_project": "qa-claim-home",
            "milestone_id": milestone_id,
        }),
        project="qa-claim-target",
    )
    ok(completed.get("status") == "In Review", "complete_claim moves linked task to In Review")
    mission = completed.get("mission") or {}
    progress = mission.get("progress") or {}
    ok(progress.get("in_review_count") == 1 and progress.get("done_with_proof_count") == 0,
       "mission progress counts In Review separately from Done-with-proof")
    ok(mission.get("deliverable_status") == "in_review",
       "deliverable status moves to in_review when linked work enters review")

    done_requested = store.complete_claim(
        "taskclaim-nonexistent",
        evidence=json.dumps({"head_sha": "def456", "final_status": "Done"}),
        final_status="Done",
        project="qa-claim-target",
    )
    ok(done_requested.get("error") == "claim not found",
       "complete_claim on inactive claim fails closed")
    refreshed = store.get_deliverable("claim-mission", project="qa-claim-home")
    ok((refreshed.get("progress") or {}).get("done_with_proof_count") == 0,
       "Done-with-proof count ignores agent completion without terminal provenance")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
