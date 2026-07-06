#!/usr/bin/env python3
"""DELIVERABLES-8: dogfood mission fixtures and mission-page exit criteria."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="deliverables-dogfood-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deliverable_dogfood_fixtures as fixtures  # noqa: E402
import mission_narrative  # noqa: E402
import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    TestClient = None  # type: ignore
    app = None  # type: ignore
    _optional_dep = exc.name
else:
    _optional_dep = None

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _linked_project_ids(status):
    return sorted({
        link.get("project_id")
        for link in (status.get("linked_tasks") or [])
        if link.get("project_id")
    })


def _assert_mission_cockpit(status, *, home_project, deliverable_id,
                            min_links, expect_narrative=False,
                            expect_blockers=False, expect_done=False,
                            expect_active=False, expect_next_actions=False):
    ok(status.get("schema") == "switchboard.mission_status.v1",
       f"{deliverable_id}: mission_status schema")
    ok(status.get("project_id") == home_project,
       f"{deliverable_id}: owning project is {home_project}")
    ok(status.get("deliverable_id") == deliverable_id,
       f"{deliverable_id}: deliverable id resolves")
    ok(bool((status.get("deliverable") or {}).get("end_state")),
       f"{deliverable_id}: end_state present")
    ok(len(status.get("milestones") or []) >= 1,
       f"{deliverable_id}: milestones render")
    ok(len(status.get("linked_tasks") or []) >= min_links,
       f"{deliverable_id}: cross-project links ({min_links}+)")
    ok(isinstance(status.get("blockers"), list),
       f"{deliverable_id}: blockers list present")
    ok(isinstance(status.get("next_actions"), list),
       f"{deliverable_id}: next_actions list present")
    ok(isinstance(status.get("active_work"), list),
       f"{deliverable_id}: active_work list present")
    ok(isinstance(status.get("done_with_proof"), list),
       f"{deliverable_id}: done_with_proof list present")
    if expect_narrative:
        ok(bool(status.get("narrative")),
           f"{deliverable_id}: narrative present")
    if expect_blockers:
        ok(len(status.get("blockers") or []) > 0,
           f"{deliverable_id}: blockers surfaced")
    if expect_done:
        ok(len(status.get("done_with_proof") or []) > 0,
           f"{deliverable_id}: Done-with-proof surfaced")
    if expect_active:
        ok(len(status.get("active_work") or []) > 0,
           f"{deliverable_id}: active work surfaced")
    if expect_next_actions:
        ok(len(status.get("next_actions") or []) > 0,
           f"{deliverable_id}: next actions surfaced")


def _assert_no_cross_project_writes(meta):
    home = meta["home_project"]
    leaks = fixtures.assert_no_cross_project_deliverable_leak(
        store, home, fixtures.linked_projects_for_fixture(meta))
    ok(not leaks, f"{meta['fixture']}: no deliverable leak into linked projects"
       + (f" ({'; '.join(leaks)})" if leaks else ""))


try:
    store.init_project_registry()
    seeded = fixtures.seed_all_dogfood_fixtures(store, actor="test")
    ok(len(seeded) == 4, "seed_all_dogfood_fixtures returns four fixtures")

    qa = seeded["qa_scratch"]
    _assert_no_cross_project_writes(qa)
    qa_status = store.get_mission_status(project=qa["home_project"],
                                         deliverable_id=qa["deliverable_id"])
    _assert_mission_cockpit(
        qa_status,
        home_project=qa["home_project"],
        deliverable_id=qa["deliverable_id"],
        min_links=2,
        expect_narrative=True,
        expect_blockers=True,
        expect_active=True,
        expect_next_actions=True,
    )
    ok(_linked_project_ids(qa_status) == sorted(qa["linked_projects"]),
       "qa scratch links qa2scratch20260702a and qa2target20260702a")
    ok((qa_status.get("progress") or {}).get("linked_task_count") == 2,
       "qa scratch progress counts both linked tasks")

    helm = seeded["helm_renderer"]
    _assert_no_cross_project_writes(helm)
    store.claim_task(helm["active_task_id"], "dogfood-agent", actor="test",
                     project=fixtures.HELM_HOME)
    helm_status = store.get_mission_status(project=helm["home_project"],
                                           deliverable_id=helm["deliverable_id"])
    _assert_mission_cockpit(
        helm_status,
        home_project=helm["home_project"],
        deliverable_id=helm["deliverable_id"],
        min_links=3,
        expect_narrative=True,
        expect_blockers=True,
        expect_done=True,
        expect_active=True,
        expect_next_actions=True,
    )
    ok(_linked_project_ids(helm_status) == sorted(helm["linked_projects"]),
       "helm renderer links helmrenderer, helm, and vulkan")
    ok(any(item.get("task_id") == helm["done_task_id"]
           for item in (helm_status.get("done_with_proof") or [])),
       "helm renderer shows vulkan proof as Done-with-proof")
    ok(any(item.get("task_id") == helm["active_task_id"]
           for item in (helm_status.get("active_work") or [])),
       "helm renderer shows active renderer work")

    access = seeded["access_rollout"]
    _assert_no_cross_project_writes(access)
    access_status = store.get_mission_status(project=access["home_project"],
                                             deliverable_id=access["deliverable_id"])
    _assert_mission_cockpit(
        access_status,
        home_project=access["home_project"],
        deliverable_id=access["deliverable_id"],
        min_links=3,
        expect_narrative=True,
        expect_blockers=True,
        expect_active=True,
        expect_next_actions=True,
    )
    workstreams = sorted({
        (link.get("task_detail") or {}).get("workstream")
        for link in (access_status.get("linked_tasks") or [])
    })
    ok(workstreams == ["ACCESS", "HARDEN", "QA"],
       "access rollout spans ACCESS, HARDEN, and QA workstreams")

    stale = seeded["stale_blocked"]
    _assert_no_cross_project_writes(stale)
    stale_status = store.get_mission_status(project=stale["home_project"],
                                            deliverable_id=stale["deliverable_id"])
    _assert_mission_cockpit(
        stale_status,
        home_project=stale["home_project"],
        deliverable_id=stale["deliverable_id"],
        min_links=1,
        expect_narrative=True,
        expect_blockers=True,
    )
    flags = (stale_status.get("narrative_state") or {}).get("flags") or []
    ok("optimistic_manual_narrative" in flags,
       "stale/blocked fixture flags optimistic manual narrative")
    ok(any(b.get("kind") == "deliverable_blocked"
           for b in (stale_status.get("blockers") or [])),
       "stale/blocked fixture surfaces deliverable_blocked blocker")
    ok(any(b.get("kind") == "task_blocked"
           for b in (stale_status.get("blockers") or [])),
       "stale/blocked fixture surfaces task_blocked blocker")
    stale_narrative = mission_narrative.narrative_state(
        stale_status,
        metadata=store.get_deliverable(stale["deliverable_id"],
                                       project=stale["home_project"]).get("metadata"),
        stored_brief=stale.get("generated_brief"),
    )
    ok(stale_narrative.get("stale")
       and "generated_brief_stale" in (stale_narrative.get("flags") or []),
       "stale/blocked fixture flags generated brief stale after durable change")

    for key, meta in seeded.items():
        listed = store.list_deliverables(project=meta["home_project"])
        ok(any(row.get("id") == meta["deliverable_id"] for row in listed),
           f"{key}: deliverable list includes fixture")

    if TestClient is None:
        ok(True, f"SKIP REST mission page proof (optional dependency: {_optional_dep})")
    else:
        client = TestClient(app)
        for key, meta in seeded.items():
            res = client.get(
                f"/api/deliverables/{meta['deliverable_id']}/mission_status",
                params={"project": meta["home_project"]},
            )
            ok(res.status_code == 200,
               f"{key}: REST mission_status returns 200")
            body = res.json()
            ok(body.get("schema") == "switchboard.mission_status.v1",
               f"{key}: REST mission_status schema")
            ok(len(body.get("linked_tasks") or []) >= 1,
               f"{key}: REST mission_status includes linked tasks")

        listed = client.get("/api/deliverables", params={"project": qa["home_project"]})
        ok(listed.status_code == 200 and len(listed.json().get("deliverables") or []) >= 1,
           "REST deliverables list includes dogfood fixture")

        brief_res = client.post(
            f"/api/deliverables/{helm['deliverable_id']}/mission_brief",
            params={"project": helm["home_project"]},
        )
        ok(brief_res.status_code == 200,
           "REST mission_brief generation succeeds for helm dogfood fixture")
        brief_body = brief_res.json()
        ok((brief_body.get("mission_brief") or {}).get("schema") == "switchboard.mission_brief.v1",
           "REST mission_brief returns structured brief")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nDeliverables dogfood proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
