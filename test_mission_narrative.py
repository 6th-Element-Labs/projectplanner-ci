#!/usr/bin/env python3
"""DELIVERABLES-6: structured mission brief generation and stale narrative flags."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mission-narrative-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mission_narrative  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    home = store.create_project("Brief Home", project_id="qa-brief-home", actor="test")
    target = store.create_project("Brief Target", project_id="qa-brief-target", actor="test")
    ok(home.get("created") and target.get("created"), "projects created")

    board = store.create_project_board(
        {"id": "webgpu-mission", "title": "WebGPU Mission", "kind": "mission", "status": "active",
         "end_state": "Helm renders with WebGPU visible to users."},
        actor="test", project="qa-brief-home",
    )
    deliverable = store.create_deliverable(
        {"id": "webgpu-mission", "board_id": board["id"], "title": "WebGPU Mission",
         "status": "in_progress", "end_state": "Helm renders with WebGPU visible to users.",
         "why_it_matters": "Cross-board renderer outcome must be visible to operators.",
         "policy_constraints": {"renderer": "webgpu"}},
        actor="test", project="qa-brief-home",
    )
    milestone = store.add_deliverable_milestone(
        deliverable["id"], {"title": "Build ingest", "status": "in_progress"},
        actor="test", project="qa-brief-home",
    )
    task = store.create_task(
        {"workstream_id": "RENDER", "title": "WebGPU ingest path", "status": "Not Started"},
        actor="test", project="qa-brief-target",
    )
    store.link_task_to_deliverable(
        deliverable["id"], "qa-brief-target", task["task_id"],
        milestone_id=milestone["milestones"][0]["id"],
        data={"blocks_deliverable": True}, actor="test", project="qa-brief-home",
    )

    status = store.get_mission_status(project="qa-brief-home", deliverable_id=deliverable["id"])
    ok(status.get("schema") == "switchboard.mission_status.v1",
       "mission status remains primary contract")
    ok(isinstance(status.get("narrative_state"), dict),
       "mission status exposes narrative_state")

    generated = store.generate_mission_brief(
        project="qa-brief-home", deliverable_id=deliverable["id"], actor="test")
    brief = generated.get("mission_brief") or {}
    ok(brief.get("schema") == "switchboard.mission_brief.v1",
       "generate_mission_brief returns structured brief")
    ok(brief.get("sections", {}).get("what_we_are_building", {}).get("text"),
       "brief includes what we are building")
    ok("No linked tasks are Done with merge" in brief.get("sections", {}).get("completed_proof", {}).get("text", ""),
       "brief does not optimistically claim proof")
    ok(len(brief.get("citations") or []) > 0, "brief cites durable sources")

    store.update_mission_narrative(
        deliverable["id"], "We are on track and shipping soon.", actor="human",
        project="qa-brief-home",
    )
    stale_status = store.get_mission_status(project="qa-brief-home", deliverable_id=deliverable["id"])
    flags = (stale_status.get("narrative_state") or {}).get("flags") or []
    ok("optimistic_manual_narrative" in flags,
       "optimistic manual narrative is flagged when blockers/no proof")

    fp1 = mission_narrative.brief_source_fingerprint(status)
    fp2 = mission_narrative.brief_source_fingerprint(stale_status)
    ok(fp1 == fp2, "fingerprint stable when underlying mission state unchanged")

    rebuilt = mission_narrative.build_mission_brief(stale_status)
    ok(rebuilt.get("source_fingerprint") == fp2,
       "rebuilt brief fingerprint matches mission status")
    state = mission_narrative.narrative_state(
        stale_status, metadata={"generated_brief": brief}, stored_brief=brief)
    ok(state.get("stale") is False or "generated_brief_stale" not in (state.get("flags") or []),
       "fresh generated brief is not marked stale immediately")

    store.update_task(task["task_id"], {"status": "Blocked"}, actor="test", project="qa-brief-target")
    changed = store.get_mission_status(project="qa-brief-home", deliverable_id=deliverable["id"])
    fp3 = mission_narrative.brief_source_fingerprint(changed)
    ok(fp3 != fp1, "fingerprint changes when linked task state changes")
    stale_after = mission_narrative.narrative_state(
        changed, metadata={"generated_brief": brief}, stored_brief=brief)
    ok(stale_after.get("stale") and "generated_brief_stale" in (stale_after.get("flags") or []),
       "stored brief flagged stale after durable event change")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
