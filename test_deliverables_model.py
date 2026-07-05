#!/usr/bin/env python3
"""Self-contained tests for Switchboard deliverable/mission data model."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="deliverables-model-")
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
    home = store.create_project("Deliverables Home", project_id="qa-deliv-home",
                                actor="test")
    target = store.create_project("Deliverables Target", project_id="qa-deliv-target",
                                  actor="test")
    ok(home.get("created") is True and target.get("created") is True,
       "test projects are physically created")

    target_task = store.create_task(
        {
            "workstream_id": "RENDER",
            "title": "WebGPU ingest path",
            "description": "Consume shared render-model fixtures in the browser.",
        },
        actor="test",
        project="qa-deliv-target",
    )
    ok(target_task["task_id"] == "RENDER-1", "linked task exists on target project")

    mission = store.create_project_board(
        {
            "id": "helm-cpp-webgpu-renderer",
            "title": "Helm C++ + WebGPU Renderer",
            "kind": "mission",
            "status": "active",
            "purpose": "Coordinate cross-board chart-renderer work as one visible outcome.",
            "end_state": (
                "Helm renders chart layers in the browser from shared C++ "
                "nautical semantics with WebGPU visible to users."
            ),
        },
        actor="test",
        project="qa-deliv-home",
    )
    ok(mission["id"] == "helm-cpp-webgpu-renderer" and
       mission["project_id"] == "qa-deliv-home",
       "mission board is created as a child of the owning project")
    ok(store.list_project_boards(project="qa-deliv-home")[0]["id"] == mission["id"],
       "project board list returns mission children")

    unknown_board_deliverable = store.create_deliverable(
        {
            "id": "bad-board-deliverable",
            "title": "Bad board deliverable",
            "board_id": "missing-board",
        },
        actor="test",
        project="qa-deliv-home",
    )
    ok(unknown_board_deliverable.get("error") == "unknown board",
       "deliverable creation fails closed on unknown board id")

    deliverable = store.create_deliverable(
        {
            "id": "helm-cpp-webgpu-renderer",
            "board_id": mission["id"],
            "title": "Helm C++ + WebGPU Renderer",
            "status": "approved",
            "owner_org": "6th Element Labs",
            "owner_person_or_role": "Product owner",
            "end_state": (
                "Helm renders chart layers in the browser from shared C++ "
                "nautical semantics with WebGPU visible to users."
            ),
            "why_it_matters": "Humans steer outcomes while agents execute board tasks.",
            "confidence": 0.42,
            "acceptance_criteria": [
                "cross-board tasks roll up into one mission",
                "green means merged/proven",
            ],
            "policy_constraints": {"runtime_language": "c++", "renderer": "webgpu"},
            "proof_requirements": {"merge_provenance": True, "fixture_parity": True},
        },
        actor="test",
        project="qa-deliv-home",
    )
    ok(deliverable["id"] == "helm-cpp-webgpu-renderer",
       "deliverable record is created in owning project")
    ok(deliverable["board_id"] == mission["id"] and
       deliverable["board"]["title"] == mission["title"],
       "deliverable carries first-class board/mission context")
    ok(deliverable["policy_constraints"]["runtime_language"] == "c++",
       "structured policy constraints survive round trip")

    updated_without_board = store.create_deliverable(
        {
            "id": "helm-cpp-webgpu-renderer",
            "title": "Helm C++ + WebGPU Renderer",
            "status": "in_progress",
        },
        actor="test",
        project="qa-deliv-home",
    )
    ok(updated_without_board["board_id"] == mission["id"],
       "deliverable upsert without board_id preserves existing mission link")

    with_milestone = store.add_deliverable_milestone(
        "helm-cpp-webgpu-renderer",
        {
            "title": "Build WebGPU ingest",
            "status": "in_progress",
            "acceptance_criteria": ["fixture loads", "no blank frame"],
            "proof_requirements": {"test": "fixture parity"},
        },
        actor="test",
        project="qa-deliv-home",
    )
    milestone_id = with_milestone["milestones"][0]["id"]
    ok(milestone_id == "helm-cpp-webgpu-renderer:build-webgpu-ingest",
       "milestone id is scoped under deliverable id")

    linked = store.link_task_to_deliverable(
        "helm-cpp-webgpu-renderer",
        "qa-deliv-target",
        "RENDER-1",
        milestone_id=milestone_id,
        data={
            "board_id": mission["id"],
            "role": "implementation",
            "blocks_deliverable": True,
            "proof_required": {"merged_sha": True},
        },
        actor="test",
        project="qa-deliv-home",
    )
    ok(linked["task_links"][0]["project_id"] == "qa-deliv-target",
       "task link stores explicit linked project id")
    ok(linked["task_links"][0]["board_id"] == mission["id"],
       "task link inherits explicit mission id")
    ok(linked["task_links"][0]["task"]["title"] == "WebGPU ingest path",
       "get_deliverable can read linked task snapshot by explicit project")
    ok(linked["progress"]["linked_task_count"] == 1 and
       linked["progress"]["done_with_proof_count"] == 0,
       "mission progress rolls up linked tasks without optimism")

    target_after = store.get_task("RENDER-1", project="qa-deliv-target")
    ok(target_after["title"] == target_task["title"] and
       target_after.get("deliverable") is None,
       "linking does not mutate or cross-pollute the target task")
    ok(store.get_deliverable("helm-cpp-webgpu-renderer",
                             project="qa-deliv-target") is None,
       "deliverable does not leak into linked project database")

    unknown_project = store.link_task_to_deliverable(
        "helm-cpp-webgpu-renderer",
        "missing-project",
        "RENDER-1",
        project="qa-deliv-home",
    )
    ok(unknown_project.get("error", "").startswith("unknown linked project"),
       "unknown linked project fails closed")

    missing_task = store.link_task_to_deliverable(
        "helm-cpp-webgpu-renderer",
        "qa-deliv-target",
        "RENDER-404",
        project="qa-deliv-home",
    )
    ok(missing_task.get("error") == "unknown linked task",
       "unknown linked task fails closed")

    board_mismatch = store.link_task_to_deliverable(
        "helm-cpp-webgpu-renderer",
        "qa-deliv-target",
        "RENDER-1",
        data={"board_id": "missing-board"},
        project="qa-deliv-home",
    )
    ok(board_mismatch.get("error") == "unknown board",
       "task link fails closed on unknown board id")

    bad_confidence = store.create_deliverable(
        {"title": "Bad confidence", "confidence": "not-a-number"},
        project="qa-deliv-home",
    )
    ok(bad_confidence.get("error") == "confidence must be a number between 0 and 1",
       "invalid confidence fails early")

    listed = store.list_deliverables(project="qa-deliv-home")
    ok(len(listed) == 1 and listed[0]["id"] == "helm-cpp-webgpu-renderer",
       "list_deliverables returns the mission")
    scoped = store.list_deliverables(project="qa-deliv-home", board_id=mission["id"])
    ok(len(scoped) == 1 and scoped[0]["board_id"] == mission["id"],
       "list_deliverables can scope by mission board id")

    export = store.audit_export(project="qa-deliv-home")
    ok(export["summary"]["project_board_count"] == 1 and
       export["summary"]["deliverable_count"] == 1 and
       export["deliverables"]["boards"][0]["id"] == mission["id"] and
       export["deliverables"]["task_links"][0]["task_id"] == "RENDER-1",
       "audit export includes project boards, deliverable records, and links")

    status = store.get_mission_status(project="qa-deliv-home",
                                      deliverable_id="helm-cpp-webgpu-renderer")
    ok(status.get("schema") == "switchboard.mission_status.v1" and
       status.get("deliverable_id") == "helm-cpp-webgpu-renderer" and
       bool((status.get("deliverable") or {}).get("end_state")),
       "mission status resolves deliverable end state")
    ok(len(status.get("linked_tasks") or []) == 1 and
       status["progress"]["linked_task_count"] == 1,
       "mission status includes linked task rollup")
    ok(any(a.get("action") == "claim_task" for a in status.get("next_actions") or []),
       "mission status suggests claim for ready linked task")

    store.report_usage(source="agent_report", confidence="reported",
                       task_id="RENDER-1", cost_usd=2.0,
                       prompt_tokens=200, completion_tokens=50,
                       project="qa-deliv-target")
    proven_outcome = store.record_outcome("feature", "WebGPU ingest shipped",
                                          task_id="RENDER-1", project="qa-deliv-target")
    store.verify_outcome(proven_outcome["id"], verifier="test",
                         verification="fixture parity", project="qa-deliv-target")
    target_kpi = store.create_kpi("renderer milestones", "milestone", "increase",
                                    project="qa-deliv-target")
    store.link_outcome_to_kpi(proven_outcome["id"], target_kpi["id"], contribution=1,
                              confidence="measured", project="qa-deliv-target")
    review_task = store.create_task(
        {"workstream_id": "RENDER", "title": "Shader parity review",
         "status": "In Review"},
        actor="test",
        project="qa-deliv-target",
    )
    store.link_task_to_deliverable(
        "helm-cpp-webgpu-renderer",
        "qa-deliv-target",
        review_task["task_id"],
        milestone_id=milestone_id,
        data={"role": "review", "board_id": mission["id"]},
        actor="test",
        project="qa-deliv-home",
    )
    store.report_usage(source="agent_report", confidence="reported",
                       task_id=review_task["task_id"], cost_usd=1.5,
                       prompt_tokens=100, completion_tokens=25,
                       project="qa-deliv-target")
    store.mark_task_merged("RENDER-1", "deadbeef" * 5, pr_number=42,
                           project="qa-deliv-target")

    economics = store.deliverable_tally("helm-cpp-webgpu-renderer",
                                        project="qa-deliv-home")
    ok(economics.get("schema") == "switchboard.deliverable_tally.v1",
       "deliverable tally exposes schema")
    ok(economics["totals"]["combined"]["spend"]["cost_usd"] == 3.5,
       "deliverable tally combines spend across linked tasks")
    ok(economics["totals"]["proven"]["spend"]["cost_usd"] == 2.0,
       "deliverable tally separates proven spend")
    ok(economics["totals"]["in_review"]["spend"]["cost_usd"] == 1.5,
       "deliverable tally separates in-review spend")
    ok(economics["totals"]["proven"]["verified_outcomes"] == 1,
       "proven bucket counts verified outcomes on merged tasks")
    ok(any(t["task_id"] == "RENDER-1" and t["proof_bucket"] == "proven"
           for t in economics["by_task"]),
       "task economics include proof bucket")
    ok(any(m.get("milestone_id") == milestone_id and
           m["combined"]["spend"]["cost_usd"] >= 3.5
           for m in economics["by_milestone"]),
       "milestone economics roll up linked task spend")
    ok(any(k.get("kpi_id") == target_kpi["id"] for k in economics["kpis"]),
       "deliverable tally includes cross-project KPI movement")

    status_with_econ = store.get_mission_status(project="qa-deliv-home",
                                                deliverable_id="helm-cpp-webgpu-renderer")
    ok((status_with_econ.get("economics") or {}).get("schema") ==
       "switchboard.deliverable_tally.v1",
       "mission status embeds deliverable economics")
    ok(status_with_econ["economics"]["totals"]["combined"]["unit_cost"]
       ["cost_per_verified_outcome"] == 3.5,
       "mission status economics expose combined cost per verified outcome")
    ok(status_with_econ["economics"]["totals"]["proven"]["unit_cost"]
       ["cost_per_verified_outcome"] == 2.0,
       "mission status economics expose proven cost per verified outcome")

    board_status = store.get_mission_status(project="qa-deliv-home",
                                            board_id=mission["id"])
    ok(board_status.get("deliverable_id") == "helm-cpp-webgpu-renderer",
       "mission status resolves deliverable from board/mission id")

    narrative = store.update_mission_narrative(
        "helm-cpp-webgpu-renderer",
        "Cross-board renderer mission is active.",
        actor="test",
        project="qa-deliv-home",
    )
    ok((narrative.get("metadata") or {}).get("narrative") ==
       "Cross-board renderer mission is active.",
       "mission narrative is stored on deliverable metadata")

    proposal = store.propose_deliverable_breakdown(
        "helm-cpp-webgpu-renderer",
        {
            "milestones": [{
                "title": "Prove parity",
                "tasks": [{
                    "project_id": "qa-deliv-target",
                    "workstream_id": "RENDER",
                    "title": "Fixture parity gate",
                }],
            }],
        },
        actor="test",
        project="qa-deliv-home",
    )
    ok(proposal.get("tasks_created") is False and
       proposal.get("proposal", {}).get("status") == "proposed",
       "breakdown proposal stores draft without creating tasks")
    target_before = store.list_tasks(project="qa-deliv-target")
    ok(len(target_before) == 2, "proposal alone does not create target tasks")

    approved = store.approve_deliverable_breakdown(
        proposal["proposal"]["id"], actor="test", project="qa-deliv-home")
    ok(len(approved.get("created_tasks") or []) == 1,
       "approved breakdown creates and links proposed tasks")
    target_after = store.list_tasks(project="qa-deliv-target")
    ok(len(target_after) == 3, "approved breakdown creates task on target project")

    unlinked = store.unlink_task_from_deliverable(
        "helm-cpp-webgpu-renderer",
        "qa-deliv-target",
        "RENDER-1",
        actor="test",
        project="qa-deliv-home",
    )
    ok(len(unlinked.get("task_links") or []) == 2 and
       all(l.get("task_id") != "RENDER-1" for l in unlinked["task_links"]),
       "unlink removes one task link without deleting the task")
    ok(store.get_task("RENDER-1", project="qa-deliv-target") is not None,
       "unlink does not delete the linked task")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
