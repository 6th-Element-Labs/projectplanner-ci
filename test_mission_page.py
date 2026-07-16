#!/usr/bin/env python3
"""DELIVERABLES-5: prove mission page REST contract used by the operator UI."""
import json
import os
import shutil
import sys
import tempfile
import time
from scripts.frontend_test_source import read_frontend_source

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

    runner_agent = "codex/SESSION-13-mission-proof"
    runner_claim = store.claim_task(
        target_task["task_id"], runner_agent, actor="test", project=TARGET)
    store.upsert_runner_session({
        "runner_session_id": "runner-session-13-mission",
        "host_id": "host/mission-proof",
        "agent_id": runner_agent,
        "runtime": "codex",
        "task_id": target_task["task_id"],
        "claim_id": runner_claim["claim_id"],
        "status": "running",
        "cwd": "/tmp/session-13-mission",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3"},
        "metadata": {"wake_id": "wake-session-13-mission",
                     "work_session_id": "worksession-session-13-mission"},
        "heartbeat_ttl_s": 1800,
    }, actor="test", project=TARGET)
    pointer_status = client.get(
        f"/api/deliverables/{deliverable['id']}/mission_status",
        params={"project": HOME},
    ).json()
    pointer_work = next(
        item for item in pointer_status.get("active_work") or []
        if item.get("task_id") == target_task["task_id"])
    pointer_runner = pointer_work.get("active_runner") or {}
    ok(pointer_runner.get("active") is True
       and pointer_runner.get("source") == "agent_state_pointer"
       and (pointer_runner.get("session") or {}).get("host_id") == "host/mission-proof",
       "Mission active_work resolves the registered runner through agent_state")

    with store._conn(TARGET) as c:
        c.execute("UPDATE tasks SET agent_state='{}', updated_at=? WHERE task_id=?",
                  (time.time(), target_task["task_id"]))
    fallback_status = client.get(
        f"/api/deliverables/{deliverable['id']}/mission_status",
        params={"project": HOME},
    ).json()
    fallback_work = next(
        item for item in fallback_status.get("active_work") or []
        if item.get("task_id") == target_task["task_id"])
    ok((fallback_work.get("active_runner") or {}).get("source")
       == "runner_sessions_fallback",
       "Mission active_work falls back to authoritative runner_sessions")

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

    # --- UI-1: the mission page is now an editable planning surface. Prove every REST
    # contract the operator UI drives to author the deliverable graph — no MCP/CLI. ---
    ms2 = client.post(
        f"/api/deliverables/{deliverable['id']}/milestones",
        params={"project": HOME},
        json={"title": "Wire access UI", "status": "not_started",
              "acceptance_criteria": ["Login form renders", "Session cookie set"]},
    )
    ok(ms2.status_code == 200, "POST milestone (add) returns 200")
    ok(len((ms2.json() or {}).get("milestones") or []) == 2,
       "add_deliverable_milestone REST appended a second milestone")

    second_task = store.create_task(
        {"workstream_id": "ACCESS", "title": "Access UI shell"},
        actor="test", project=TARGET)
    link2 = client.post(
        f"/api/deliverables/{deliverable['id']}/task_links",
        params={"project": HOME},
        json={"task_project": TARGET, "task_id": second_task["task_id"],
              "role": "contributes", "blocks_deliverable": False},
    )
    ok(link2.status_code == 200, "POST task_links (link) returns 200")
    ok(any(l.get("task_id") == second_task["task_id"]
           for l in ((link2.json() or {}).get("task_links") or [])),
       "link_task_to_deliverable REST attached the task")

    unlink2 = client.delete(
        f"/api/deliverables/{deliverable['id']}/task_links",
        params={"project": HOME, "task_project": TARGET,
                "task_id": second_task["task_id"]},
    )
    ok(unlink2.status_code == 200, "DELETE task_links (unlink) returns 200")

    outcome_res = client.post(
        f"/api/deliverables/{deliverable['id']}/outcome",
        params={"project": HOME},
        json={"outcome": "Operators self-serve access from the web.",
              "target_projects": [TARGET],
              "acceptance_criteria": ["No CLI needed"]},
    )
    ok(outcome_res.status_code == 200, "POST outcome returns 200")
    outcome_body = outcome_res.json() or {}
    proposal = outcome_body.get("proposal") or outcome_body
    proposal_id = proposal.get("id") or proposal.get("proposal_id")
    ok(bool(proposal_id),
       "submit_deliverable_outcome REST drafted a breakdown proposal")

    proposals = client.get(
        "/api/deliverables/breakdown_proposals",
        params={"project": HOME, "deliverable_id": deliverable["id"]},
    )
    ok(proposals.status_code == 200, "GET breakdown_proposals returns 200")
    plist = (proposals.json() or {}).get("proposals") or []
    ok(any(p.get("id") == proposal_id for p in plist),
       "breakdown review card can list the pending proposal")

    if proposal_id:
        rej = client.post(
            f"/api/deliverables/breakdown_proposals/{proposal_id}/reject",
            params={"project": HOME}, json={"reason": "superseded by manual plan"})
        rej_status = ((rej.json() or {}).get("proposal") or {}).get("status")
        ok(rej.status_code == 200 and rej_status == "rejected",
           "POST breakdown reject transitions the proposal")

    index = client.get("/")
    ok(index.status_code == 200 and "tab-mission" in index.text,
       "index.html exposes Mission tab shell")
    ok("mission-generate-brief" in index.text,
       "index.html exposes generate brief control")
    ok("mission-page" in index.text and "mission-deliverable-picker" in index.text,
       "index.html exposes mission page containers")
    for shell in ("dl-link-modal", "dl-milestone-modal", "dl-outcome-modal", "dl-node-modal"):
        ok(shell in index.text, f"index.html exposes {shell} authoring modal")

    app_js = read_frontend_source(os.path.dirname(__file__))
    for needle in (
        "refreshMissionPage",
        "renderMissionPage",
        "loadMissionStatus",
        "generateMissionBrief",
        "_missionBriefHtml",
        "openLinkedTask",
        "_missionPolicyDrift",
        "_missionNodeTooltip",
        # UI-1 authoring surface
        "loadBreakdownProposals",
        "_missionBreakdownHtml",
        "openLinkModal",
        "submitLinkTask",
        "openMilestoneModal",
        "submitMilestone",
        "openOutcomeModal",
        "submitOutcome",
        "openNodeModal",
        "submitNodeLink",
        "unlinkNode",
        "approveProposal",
        "rejectProposal",
        "deferProposal",
    ):
        ok(needle in app_js, f"app.js defines {needle}")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nMission page proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
