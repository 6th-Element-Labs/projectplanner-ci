#!/usr/bin/env python3
"""Seed real, focused deliverables so the Mission/Deliverable tab has data.

The deliverable/mission cockpit had no seed data on any live board, so the tab
rendered an empty state. This links each board's *existing* tasks (real
depends_on, real statuses) into one coherent deliverable per board, so the
cockpit KPIs, milestones and dependency graph all light up. Idempotent:
re-running reuses deliverables/milestones and links only new tasks.

Run:  .venv/bin/python scripts/seed_helm_mission_demo.py
View: pick the board in the switcher, open the Deliverable tab, e.g.
      ?project=helm&deliverable=helm-chart-render#tab-mission
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store

ACTOR = "seed/mission-demo"

# One focused deliverable per live board, built from that board's real tasks.
# workstreams -> one milestone each; every task in those workstreams gets linked.
DELIVERABLES = [
    {
        "project": "helm",
        "id": "helm-chart-render",
        "title": "Chart & rendering pipeline",
        "end_state": (
            "Headless S-52 ENC rendering and the OpenCPN nav engine merged into one "
            "binary — tiles served, quilting deterministic, palettes and object query live."
        ),
        "why_it_matters": (
            "The chart is the product. Everything the mariner sees renders on the S-52 "
            "pipeline, so this deliverable gates the entire on-water experience."
        ),
        "confidence": 0.62,
        "milestones": [
            ("Nav engine core", "ENGINE"),
            ("S-52 chart rendering", "CHART"),
        ],
    },
    {
        "project": "switchboard",
        "id": "switchboard-coordination",
        "title": "Agent coordination dogfood",
        "end_state": (
            "Agents claim work, dispatch to Claude Code, reconcile provenance and enforce "
            "PR gates on the live Switchboard board without human babysitting."
        ),
        "why_it_matters": (
            "Switchboard has to run its own plan to prove the coordination layer works — "
            "if it can't dogfood itself, it can't be trusted on customer boards."
        ),
        "confidence": 0.5,
        "milestones": [
            ("Dogfood loop", "DOGFOOD"),
            ("Dispatch & reconcile", "DISPATCH"),
            ("Provenance reconcile", "RECON"),
            ("PR-gate enforcement", "ENFORCE"),
        ],
    },
    {
        "project": "maxwell",
        "id": "maxwell-cutover",
        "title": "Barnett pilot cutover",
        "end_state": (
            "TEEP Barnett Phase-1 pilot cuts over to the new stack: data migrated, "
            "reconciliations green, and operators running on the live gateway."
        ),
        "why_it_matters": (
            "Cutover is the moment the pilot becomes real. Every earlier workstream exists "
            "to make this switch safe and reversible."
        ),
        "confidence": 0.4,
        "milestones": [
            ("Cutover execution", "CUTOVER"),
        ],
    },
]


def _ensure_deliverable(spec: dict) -> dict:
    project, did = spec["project"], spec["id"]
    existing = store.get_deliverable(did, project=project)
    if existing and not existing.get("error"):
        print(f"  [{project}] deliverable {did} exists — reusing")
        return existing
    board = store.create_project_board(
        {
            "id": did,
            "title": spec["title"],
            "kind": "mission",
            "status": "active",
            "end_state": spec["end_state"],
        },
        actor=ACTOR,
        project=project,
    )
    deliverable = store.create_deliverable(
        {
            "id": did,
            "board_id": board["id"],
            "title": spec["title"],
            "status": "in_progress",
            "end_state": spec["end_state"],
            "why_it_matters": spec["why_it_matters"],
            "confidence": spec["confidence"],
        },
        actor=ACTOR,
        project=project,
    )
    print(f"  [{project}] created deliverable {did}")
    return deliverable


def _seed_one(spec: dict) -> None:
    project, did = spec["project"], spec["id"]
    if not store.has_project(project):
        print(f"  [{project}] project missing — skip")
        return
    deliverable = _ensure_deliverable(spec)

    existing_ms = {m.get("title"): m.get("id")
                   for m in (deliverable.get("milestones") or [])}
    ws_to_ms: dict[str, str] = {}
    for title, ws in spec["milestones"]:
        mid = existing_ms.get(title)
        if not mid:
            res = store.add_deliverable_milestone(
                did, {"title": title, "status": "in_progress"},
                actor=ACTOR, project=project,
            )
            mid = res["milestones"][-1]["id"]
            print(f"  [{project}] milestone: {title}")
        ws_to_ms[ws] = mid

    linked = {link.get("task_id") for link in (deliverable.get("task_links") or [])}
    count = 0
    for task in store.list_tasks(project=project):
        ws = task.get("_wsId") or task.get("workstream")
        tid = task.get("task_id")
        if ws not in ws_to_ms or not tid or tid in linked:
            continue
        store.link_task_to_deliverable(
            did, project, tid, milestone_id=ws_to_ms[ws],
            actor=ACTOR, project=project,
        )
        count += 1
    print(f"  [{project}] linked {count} tasks -> {did}")


def main() -> None:
    store.init_project_registry()
    for spec in DELIVERABLES:
        _seed_one(spec)
    print("== Done ==")
    for spec in DELIVERABLES:
        print(f"  {spec['project']:12} ?project={spec['project']}&deliverable={spec['id']}#tab-mission")


if __name__ == "__main__":
    main()
