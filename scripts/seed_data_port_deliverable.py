#!/usr/bin/env python3
"""Scope ADR-0018 on the board: seed deliverable `data-port-storage-abstraction`.

Creates the DATA-PORT workstream (DATA-PORT-1 … DATA-PORT-12 with the charter's
depends_on graph), the board + deliverable, the six charter milestones, and links
each task to its milestone — mirroring docs/DATA-PORT-EXECUTION.md exactly.

Statuses are left to Switchboard provenance rules: everything seeds Not Started;
DATA-PORT-1 (the charter itself) flips via merge webhook/reconcile when the
ADR-0018 PR lands. No merge provenance is faked here.

Idempotent: re-running reuses tasks/deliverable/milestones and only fills gaps.
Run (on the Switchboard host): .venv/bin/python scripts/seed_data_port_deliverable.py
View: ?project=switchboard&deliverable=data-port-storage-abstraction#tab-mission
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store

PROJECT = "switchboard"
WS = "DATA-PORT"
WS_NAME = "Backend-agnostic storage ports"
DELIVERABLE_ID = "data-port-storage-abstraction"
ACTOR = "seed/data-port-charter"

END_STATE = (
    "Application code knows business operations + project identity (metadata) only; "
    "every backend fact lives in storage/adapters/ behind declared ports; forbidden-import "
    "ratchet at ceiling 0; conformance suite (with broken-adapter oracle) proves any adapter "
    "behaviorally identical. Swapping SQLite for Postgres/Oracle/managed cloud DB is an "
    "adapter PR with zero app diffs, enforced by CI. No second adapter ships under this "
    "deliverable — ARCH-19 owns that trigger."
)

MILESTONES = [
    "charter-rails",
    "leak-zero",
    "ports-declared",
    "ratchet-locked",
    "conformance",
    "exit",
]

# (number, title, depends_on numbers, milestone) — mirrors DATA-PORT-EXECUTION.md.
TASKS = [
    (1, "Charter: ADR-0018 + DATA-PORT execution tracker", [], "charter-rails"),
    (2, "Neutral storage error taxonomy + adapter-boundary translation", [1], "charter-rails"),
    (3, "Application-query leak fixes: audit_export, project_impact, "
        "control_plane_probe, working_agreement", [2], "leak-zero"),
    (4, "Auth storage relocation; auth port drops sqlite3.Connection", [2], "leak-zero"),
    (5, "Job/observability leak fixes: background_jobs, mcp_observability", [2], "leak-zero"),
    (6, "Declare storage port Protocols for the ten port groups", [3, 4, 5], "ports-declared"),
    (7, "Consolidate all SQL under storage/adapters/sqlite/ (verbatim moves)", [6], "ports-declared"),
    (8, "Forbidden-import ratchet: sqlite3/db.* banned outside adapters, ceiling 0", [7], "ratchet-locked"),
    (9, "Port conformance suite + broken-adapter oracle", [6], "conformance"),
    (10, "SEG-7 isolation harness parameterized by adapter", [9], "conformance"),
    (11, "Backend swap playbook: parity gate, per-project cutover, rollback drill", [8, 9, 10], "exit"),
    (12, "Exit gate: machine-readable verdict (leaks 0, ratchet 0, conformance green)", [11], "exit"),
]


def _tid(n: int) -> str:
    return f"{WS}-{n}"


def _ensure_tasks() -> None:
    existing = {t.get("task_id") for t in store.list_tasks(project=PROJECT)}
    for n, title, deps, _milestone in TASKS:
        tid = _tid(n)
        if tid in existing:
            continue
        created = store.create_task(
            {"workstream_id": WS, "workstream_name": WS_NAME,
             "title": title, "status": "Not Started",
             "depends_on": [_tid(d) for d in deps], "phase": "Build"},
            actor=ACTOR, project=PROJECT,
        )
        got = (created or {}).get("task_id")
        if got != tid:
            raise SystemExit(
                f"expected {tid} but create_task yielded {got}; board already has "
                f"{WS} tasks with different numbering — clear them or adjust.")
        print(f"  created {tid}: {title}")
    print(f"  {WS}-1 … {WS}-{len(TASKS)} present (statuses follow provenance rules)")


def _ensure_deliverable() -> dict:
    existing = store.get_deliverable(DELIVERABLE_ID, project=PROJECT)
    if existing and not existing.get("error"):
        print(f"  deliverable {DELIVERABLE_ID} exists — reusing")
        return existing
    board = store.create_project_board(
        {"id": DELIVERABLE_ID, "title": "Backend-agnostic storage ports (DATA-PORT)",
         "kind": "mission", "status": "active", "end_state": END_STATE},
        actor=ACTOR, project=PROJECT,
    )
    deliverable = store.create_deliverable(
        {"id": DELIVERABLE_ID, "board_id": board["id"],
         "title": "Backend-agnostic storage ports (DATA-PORT)",
         "status": "in_progress", "end_state": END_STATE,
         "why_it_matters": "Operator requirement: the app must not care about the underlying "
                           "database — pick/choose any relational backend as an adapter "
                           "decision. Makes ARCH-19's eventual swap one adapter + gates "
                           "per bounded context instead of an app-wide migration.",
         "confidence": 0.75},
        actor=ACTOR, project=PROJECT,
    )
    print(f"  created deliverable {DELIVERABLE_ID}")
    return deliverable


def _ensure_milestones(deliverable: dict) -> dict:
    have = {m.get("title"): m.get("id") for m in (deliverable.get("milestones") or [])}
    for title in MILESTONES:
        if title in have:
            continue
        status = "in_progress" if title == "charter-rails" else "not_started"
        result = store.add_deliverable_milestone(
            DELIVERABLE_ID, {"title": title, "status": status},
            actor=ACTOR, project=PROJECT,
        )
        if result.get("error"):
            raise SystemExit(f"milestone {title!r} failed: {result}")
        print(f"  added milestone {title}")
    refreshed = store.get_deliverable(DELIVERABLE_ID, project=PROJECT)
    return {m.get("title"): m.get("id") for m in (refreshed.get("milestones") or [])}


def main() -> None:
    store.init_project_registry()
    if not store.has_project(PROJECT):
        raise SystemExit(f"project {PROJECT!r} does not exist")
    store.init_db(PROJECT)
    _ensure_tasks()
    deliverable = _ensure_deliverable()
    milestone_ids = _ensure_milestones(deliverable)

    linked = {l.get("task_id") for l in (deliverable.get("task_links") or [])}
    count = 0
    for n, _title, _deps, milestone in TASKS:
        tid = _tid(n)
        if tid in linked:
            continue
        store.link_task_to_deliverable(
            DELIVERABLE_ID, PROJECT, tid, milestone_id=milestone_ids[milestone],
            actor=ACTOR, project=PROJECT)
        count += 1
    print(f"  linked {count} tasks ({WS}-1 … {WS}-{len(TASKS)}) -> {DELIVERABLE_ID}")
    print("== Done ==")
    print(f"View: ?project={PROJECT}&deliverable={DELIVERABLE_ID}#tab-mission")


if __name__ == "__main__":
    main()
