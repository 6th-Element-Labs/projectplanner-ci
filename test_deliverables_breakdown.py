#!/usr/bin/env python3
"""Tests for deliverable breakdown coordinator workflow (DELIVERABLES-3)."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="deliverables-breakdown-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deliverable_breakdown  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_project_registry()
    home = store.create_project("Breakdown Home", project_id="qa-breakdown-home", actor="test")
    target = store.create_project("Breakdown Target", project_id="qa-breakdown-target", actor="test")
    ok(home.get("created") is True and target.get("created") is True,
       "test projects are physically created")

    existing = store.create_task(
        {"workstream_id": "RENDER", "title": "Existing ingest task"},
        actor="test",
        project="qa-breakdown-target",
    )
    ok(existing["task_id"] == "RENDER-1", "existing target task is available for link drafts")

    mission = store.create_project_board(
        {
            "id": "helm-webgpu-mission",
            "title": "Helm WebGPU Mission",
            "kind": "mission",
            "status": "active",
            "end_state": "Helm renders with WebGPU.",
        },
        actor="test",
        project="qa-breakdown-home",
    )
    deliverable = store.create_deliverable(
        {
            "id": "helm-webgpu-mission",
            "board_id": mission["id"],
            "title": "Helm WebGPU Mission",
            "status": "proposed",
            "policy_constraints": {"renderer": "webgpu"},
        },
        actor="test",
        project="qa-breakdown-home",
    )

    outcome = (
        "Make Helm render through shared C++ semantics and WebGPU with fixture parity."
    )
    submitted = store.submit_deliverable_outcome(
        deliverable["id"],
        outcome,
        actor="coordinator",
        project="qa-breakdown-home",
        target_projects=[
            {"project_id": "qa-breakdown-home", "workstream_id": "DELIVERABLES"},
            {"project_id": "qa-breakdown-target", "workstream_id": "RENDER"},
        ],
        policy_constraints={"runtime_language": "c++"},
        acceptance_criteria=["fixture parity"],
    )
    proposal = submitted.get("proposal") or {}
    ok(proposal.get("status") == "proposed" and proposal.get("outcome_text") == outcome,
       "submit outcome stores a proposed breakdown with outcome text")
    payload = proposal.get("payload") or {}
    ok(len(payload.get("milestones") or []) >= 4,
       "generated draft includes milestone groups")
    ok(all(t.get("project_id") for m in payload["milestones"] for t in m.get("tasks") or []),
       "every generated task draft carries explicit project_id")
    target_before = store.list_tasks(project="qa-breakdown-target")
    ok(len(target_before) == 1, "submit outcome does not create tasks before approval")

    edited_payload = dict(payload)
    edited_payload["milestones"][0]["tasks"] = [{
        "action": "link",
        "project_id": "qa-breakdown-target",
        "task_id": "RENDER-1",
        "role": "implementation",
    }]
    updated = store.update_deliverable_breakdown_proposal(
        proposal["id"], edited_payload, actor="human", project="qa-breakdown-home")
    ok((updated.get("proposal") or {}).get("payload", {}).get("milestones", [{}])[0]
        .get("tasks", [{}])[0].get("action") == "link",
       "human can edit pending proposal to link an existing task")

    rejected = store.reject_deliverable_breakdown(
        proposal["id"], "wrong target boards", actor="human", project="qa-breakdown-home")
    ok((rejected.get("proposal") or {}).get("status") == "rejected" and
       (rejected.get("proposal") or {}).get("review_reason") == "wrong target boards",
       "rejection is audited with reason")

    resubmitted = store.submit_deliverable_outcome(
        deliverable["id"], outcome, actor="coordinator", project="qa-breakdown-home",
        target_projects=[{"project_id": "qa-breakdown-target", "workstream_id": "RENDER"}],
    )
    proposal2 = resubmitted["proposal"]
    approved = store.approve_deliverable_breakdown(
        proposal2["id"], actor="human", project="qa-breakdown-home")
    ok(len(approved.get("created_tasks") or []) > 0,
       "approval creates tasks on explicit target projects")
    ok(len(approved.get("linked_tasks") or []) == 0,
       "create-only approval records created tasks separately from links")
    target_after = store.list_tasks(project="qa-breakdown-target")
    ok(len(target_after) > len(target_before),
       "approval creates new tasks on routed target project")
    ok((approved.get("deliverable") or {}).get("end_state") == outcome,
       "approval applies outcome to deliverable end_state")

    link_payload = deliverable_breakdown.generate_breakdown_draft(
        outcome,
        deliverable=approved["deliverable"],
        target_projects=[{"project_id": "qa-breakdown-target", "workstream_id": "RENDER"}],
        project="qa-breakdown-home",
    )
    link_payload["milestones"] = [{
        "title": "Reuse existing ingest",
        "tasks": [{
            "action": "link",
            "project_id": "qa-breakdown-target",
            "task_id": "RENDER-1",
        }],
    }]
    link_proposal = store.propose_deliverable_breakdown(
        deliverable["id"], link_payload, actor="coordinator", project="qa-breakdown-home")
    link_approved = store.approve_deliverable_breakdown(
        link_proposal["proposal"]["id"], actor="human", project="qa-breakdown-home")
    ok(len(link_approved.get("linked_tasks") or []) == 1 and
       link_approved["linked_tasks"][0]["task_id"] == "RENDER-1",
       "approval can link existing tasks without creating duplicates")

    deferred = store.submit_deliverable_outcome(
        deliverable["id"], "Deferred follow-up outcome", actor="coordinator",
        project="qa-breakdown-home",
        target_projects=[{"project_id": "qa-breakdown-home"}],
    )
    defer_result = store.defer_deliverable_breakdown(
        deferred["proposal"]["id"], "needs more product input", actor="human",
        project="qa-breakdown-home", defer_until=9999999999.0)
    ok((defer_result.get("proposal") or {}).get("status") == "deferred",
       "deferral is audited and stored")

    export = store.audit_export(project="qa-breakdown-home")
    kinds = {row.get("kind") for row in export.get("activity") or []}
    ok({"deliverable.breakdown_proposed", "deliverable.breakdown_rejected",
        "deliverable.breakdown_approved", "deliverable.breakdown_deferred",
        "deliverable.breakdown_updated"}.issubset(kinds),
       "audit export includes breakdown workflow activity kinds")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
