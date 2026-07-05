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

    deliverable = store.create_deliverable(
        {
            "id": "helm-cpp-webgpu-renderer",
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
    ok(deliverable["policy_constraints"]["runtime_language"] == "c++",
       "structured policy constraints survive round trip")

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
            "role": "implementation",
            "blocks_deliverable": True,
            "proof_required": {"merged_sha": True},
        },
        actor="test",
        project="qa-deliv-home",
    )
    ok(linked["task_links"][0]["project_id"] == "qa-deliv-target",
       "task link stores explicit linked project id")
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

    bad_confidence = store.create_deliverable(
        {"title": "Bad confidence", "confidence": "not-a-number"},
        project="qa-deliv-home",
    )
    ok(bad_confidence.get("error") == "confidence must be a number between 0 and 1",
       "invalid confidence fails early")

    listed = store.list_deliverables(project="qa-deliv-home")
    ok(len(listed) == 1 and listed[0]["id"] == "helm-cpp-webgpu-renderer",
       "list_deliverables returns the mission")

    export = store.audit_export(project="qa-deliv-home")
    ok(export["summary"]["deliverable_count"] == 1 and
       export["deliverables"]["task_links"][0]["task_id"] == "RENDER-1",
       "audit export includes deliverable records and links")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
