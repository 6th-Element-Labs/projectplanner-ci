#!/usr/bin/env python3
"""Dogfood the strategic DAG: seed the DELIVERABLES epic as a Switchboard deliverable.

Reproduces the operator's canonical example graph (DELIVERABLES-2 … DELIVERABLES-10
with strict depends_on) as a real deliverable so the dependency-graph panel renders
exactly like the reference mermaid: green = Done-with-merge-proof, grey = todo.

The "done" tasks are stamped with merge provenance (they really shipped via PRs on
master), so they render green rather than teal. D7/D8 are left todo to match the
reference snapshot; on live data they'd flip to green once their provenance lands.

Idempotent: re-running reuses the tasks + deliverable and only fills gaps.
Run:  .venv/bin/python scripts/seed_switchboard_deliverables_demo.py
View: ?project=switchboard&deliverable=switchboard-deliverables-mission#tab-mission
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store

PROJECT = "switchboard"
WS = "DELIVERABLES"
DELIVERABLE_ID = "switchboard-deliverables-mission"
ACTOR = "seed/deliverables-demo"

# (number, title, done?, depends_on numbers) — mirrors the reference mermaid exactly.
# A leading foundation task (#1) is created so numbering starts the epic at -2.
TASKS = [
    (1, "Deliverable & mission data model — schema + migrations", True, []),
    (2, "Deliverable/mission data model surfaced (REST + MCP)", True, []),
    (3, "Breakdown workflow (propose → confirm)", True, [2]),
    (4, "Deliverable-aware dispatch", True, [2]),
    (5, "Mission Page UI", True, [2]),
    (6, "Generated narrative + end-state brief", True, [5]),
    (7, "Coordinator loop", False, [3, 4, 6]),
    (8, "Dogfood missions", False, [5, 6]),
    (9, "Economics / KPI rollup", True, [2]),
    (10, "Agent startup contract", True, [4]),
]
LINKED = list(range(2, 11))          # D2 … D10 appear in the graph (D1 is the unlinked root)


def _tid(n: int) -> str:
    return f"{WS}-{n}"


def _fake_merge_sha(tid: str) -> str:
    return hashlib.sha1(f"dogfood:{tid}".encode()).hexdigest()


def _ensure_tasks() -> None:
    existing = {t.get("task_id") for t in store.list_tasks(project=PROJECT)}
    for n, title, done, deps in TASKS:
        tid = _tid(n)
        if tid not in existing:
            created = store.create_task(
                {"workstream_id": WS, "workstream_name": "Deliverables platform",
                 "title": title, "status": "Not Started",
                 "depends_on": [_tid(d) for d in deps], "phase": "Build"},
                actor=ACTOR, project=PROJECT,
            )
            got = (created or {}).get("task_id")
            if got != tid:
                raise SystemExit(
                    f"expected {tid} but create_task yielded {got}; board already has "
                    f"DELIVERABLES tasks with different numbering — clear them or adjust.")
            print(f"  created {tid}: {title}")
        # Stamp state: done -> merged (green); todo -> leave Not Started (grey).
        if done:
            store.mark_task_merged(
                tid, merged_sha=_fake_merge_sha(tid), pr_url="",
                branch="master", actor=ACTOR, project=PROJECT)
    print(f"  {WS}-1 … {WS}-10 present; done tasks stamped with merge provenance")


def _ensure_deliverable() -> dict:
    existing = store.get_deliverable(DELIVERABLE_ID, project=PROJECT)
    if existing and not existing.get("error"):
        print(f"  deliverable {DELIVERABLE_ID} exists — reusing")
        return existing
    board = store.create_project_board(
        {"id": DELIVERABLE_ID, "title": "Deliverables platform (strategic DAG)",
         "kind": "mission", "status": "active",
         "end_state": "The deliverable/mission layer ships: data model, UI, dispatch, "
                      "narrative, economics and the strategic dependency graph."},
        actor=ACTOR, project=PROJECT,
    )
    deliverable = store.create_deliverable(
        {"id": DELIVERABLE_ID, "board_id": board["id"],
         "title": "Deliverables platform (strategic DAG)", "status": "in_progress",
         "end_state": board["end_state"],
         "why_it_matters": "Dogfoods the model on its own build: one board-scoped "
                           "deliverable with the real depends_on graph the operator drew.",
         "confidence": 0.8},
        actor=ACTOR, project=PROJECT,
    )
    print(f"  created deliverable {DELIVERABLE_ID}")
    return deliverable


def main() -> None:
    store.init_project_registry()
    if not store.has_project(PROJECT):
        raise SystemExit(f"project {PROJECT!r} does not exist")
    _ensure_tasks()
    deliverable = _ensure_deliverable()
    ms = store.add_deliverable_milestone(
        DELIVERABLE_ID, {"title": "Deliverables v1", "status": "in_progress"},
        actor=ACTOR, project=PROJECT,
    ) if not (deliverable.get("milestones") or []) else {"milestones": deliverable["milestones"]}
    milestone_id = ms["milestones"][-1]["id"]

    linked = {l.get("task_id") for l in (deliverable.get("task_links") or [])}
    count = 0
    for n in LINKED:
        tid = _tid(n)
        if tid in linked:
            continue
        store.link_task_to_deliverable(
            DELIVERABLE_ID, PROJECT, tid, milestone_id=milestone_id,
            actor=ACTOR, project=PROJECT)
        count += 1
    print(f"  linked {count} tasks (DELIVERABLES-2 … DELIVERABLES-10) -> {DELIVERABLE_ID}")
    print("== Done ==")
    print(f"View: ?project={PROJECT}&deliverable={DELIVERABLE_ID}#tab-mission")


if __name__ == "__main__":
    main()
