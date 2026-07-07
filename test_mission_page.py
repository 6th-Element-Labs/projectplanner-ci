#!/usr/bin/env python3
"""DELIVERABLES-5: prove mission page REST contract used by the operator UI."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mission-page-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  mission page proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

HOME = "qa-mission-home"
TARGET = "qa-mission-target"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


client = TestClient(app)

try:
    store.init_project_registry()
    store.create_project("Mission Home", project_id=HOME, actor="test")
    store.create_project("Mission Target", project_id=TARGET, actor="test")
    store.init_db(HOME)
    store.init_db(TARGET)

    board = store.create_project_board(
        {
            "id": "switchboard-access-rollout",
            "title": "Switchboard Access rollout",
            "kind": "mission",
            "status": "active",
            "end_state": "Humans can log in with scoped project access.",
        },
        actor="test",
        project=HOME,
    )
    deliverable = store.create_deliverable(
        {
            "id": "switchboard-access-rollout",
            "board_id": board["id"],
            "title": "Switchboard Access rollout",
            "status": "in_progress",
            "end_state": "Humans can log in with scoped project access.",
            "why_it_matters": "Dogfood becomes a safe multi-human product.",
            "confidence": 0.55,
            "policy_constraints": {"auth_mode": "required"},
            "proof_requirements": {"merge_provenance": True},
        },
        actor="test",
        project=HOME,
    )
    milestone = store.add_deliverable_milestone(
        deliverable["id"],
        {"title": "Ship login shell", "status": "in_progress"},
        actor="test",
        project=HOME,
    )
    target_task = store.create_task(
        {"workstream_id": "ACCESS", "title": "Session auth"},
        actor="test",
        project=TARGET,
    )
    store.link_task_to_deliverable(
        deliverable["id"],
        TARGET,
        target_task["task_id"],
        milestone_id=milestone["milestones"][0]["id"],
        data={"role": "implementation", "blocks_deliverable": True},
        actor="test",
        project=HOME,
    )
    store.update_mission_narrative(
        deliverable["id"],
        "Access rollout is active across boards.",
        actor="test",
        project=HOME,
    )

    listed = client.get("/api/deliverables", params={"project": HOME})
    ok(listed.status_code == 200, "GET /api/deliverables returns 200")
    listed_body = listed.json()
    ok(len(listed_body.get("deliverables") or []) == 1,
       "deliverables list includes mission fixture")

    status = client.get(
        f"/api/deliverables/{deliverable['id']}/mission_status",
        params={"project": HOME},
    )
    ok(status.status_code == 200, "GET deliverable mission_status returns 200")
    body = status.json()
    ok(body.get("schema") == "switchboard.mission_status.v1",
       "mission_status schema matches UI contract")
    ok((body.get("deliverable") or {}).get("end_state"),
       "mission page can show end_state")
    ok((body.get("deliverable") or {}).get("why_it_matters"),
       "mission page can show why_it_matters")
    ok(len(body.get("milestones") or []) == 1,
       "mission page can render milestone progress map")
    ok(len(body.get("linked_tasks") or []) == 1,
       "mission page can show cross-project linked tasks")
    ok(isinstance(body.get("blockers"), list),
       "mission page can show blockers")
    ok(isinstance(body.get("next_actions"), list),
       "mission page can show next best actions")
    ok(isinstance(body.get("active_work"), list),
       "mission page can show active work")
    ok(isinstance(body.get("done_with_proof"), list),
       "mission page can show Done-with-proof")
    ok(body.get("narrative") == "Access rollout is active across boards.",
       "mission page can show live narrative")
    td = (body.get("linked_tasks") or [{}])[0].get("task_detail") or {}
    ok("narration" in td and "narration_raw" in td and "narration_stale" in td,
       "linked task_detail carries narration fields for map-node hover tooltips")
    ok(all("agent_id" in a for a in (body.get("active_agents") or [])),
       "active_agents entries are shaped for hover tooltips (agent_id + runtime enrichment)")

    brief_res = client.post(
        f"/api/deliverables/{deliverable['id']}/mission_brief",
        params={"project": HOME},
    )
    ok(brief_res.status_code == 200, "POST mission_brief returns 200")
    brief_body = brief_res.json()
    ok((brief_body.get("mission_brief") or {}).get("schema") == "switchboard.mission_brief.v1",
       "mission brief generation returns structured brief")
    ok(isinstance((brief_body.get("narrative_state") or {}), dict),
       "mission brief generation returns narrative_state")

    index = client.get("/")
    ok(index.status_code == 200 and "tab-mission" in index.text,
       "index.html exposes Mission tab shell")
    ok("mission-generate-brief" in index.text,
       "index.html exposes generate brief control")
    ok("mission-page" in index.text and "mission-deliverable-picker" in index.text,
       "index.html exposes mission page containers")

    app_js = open(os.path.join(os.path.dirname(__file__), "static", "app.js"),
                  encoding="utf-8").read()
    for needle in (
        "refreshMissionPage",
        "renderMissionPage",
        "loadMissionStatus",
        "generateMissionBrief",
        "_missionBriefHtml",
        "openLinkedTask",
        "_missionPolicyDrift",
        "_missionNodeTooltip",
    ):
        ok(needle in app_js, f"app.js defines {needle}")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nMission page proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
