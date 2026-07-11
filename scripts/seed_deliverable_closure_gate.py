#!/usr/bin/env python3
"""Seed the deliverable-closure-gate program on project=switchboard.

Creates deliverable `deliverable-closure-gate`, milestones, DELIVERABLES-12 …
DELIVERABLES-22, and links them. Idempotent.

Run:  .venv/bin/python scripts/seed_deliverable_closure_gate.py
View: ?project=switchboard&deliverable=deliverable-closure-gate#tab-mission
Spec: docs/DELIVERABLE-CLOSURE-GATE.md
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store

PROJECT = "switchboard"
WS = "DELIVERABLES"
DELIVERABLE_ID = "deliverable-closure-gate"
ACTOR = "seed/deliverable-closure-gate"

MILESTONES = [
    ("m1-spec-intake", "1 — Spec & intake", 1),
    ("m2-engine", "2 — Closure engine", 2),
    ("m3-operator", "3 — Operator surface", 3),
    ("m4-dogfood", "4 — Dogfood & ship", 4),
]

# (number, title, description, milestone_key, depends_on numbers)
TASKS = [
    (
        12,
        "Author deliverable closure gate spec + closure_report schema",
        "Write docs/DELIVERABLE-CLOSURE-GATE.md; define "
        "switchboard.deliverable_closure_report.v1 and gate 1/2 semantics.",
        "m1-spec-intake",
        [],
    ),
    (
        13,
        "Bake acceptance_criteria + proof_requirements into deliverable create flow",
        "Mission page + create_deliverable: require end_state, acceptance_criteria, "
        "proof_requirements when status moves to in_progress; validate gate refs.",
        "m1-spec-intake",
        [12],
    ),
    (
        14,
        "Gate registry manifest for harness checks",
        "deliverable_gates/manifest.json mapping gate ids to scripts/pytest/store checks; "
        "support per-deliverable overrides via proof_requirements.gates.",
        "m2-engine",
        [12],
    ),
    (
        15,
        "verify_deliverable_closure store: scope + functional gates",
        "Run scope checks (blockers, terminal tasks, waivers) and registered harness "
        "checks; produce graded closure report.",
        "m2-engine",
        [14],
    ),
    (
        16,
        "MCP/REST closure surface + deliverable.closure_verified audit",
        "verify_deliverable_closure, get_deliverable_closure_report, REST routes; "
        "persist report + activity stamp on deliverable.",
        "m2-engine",
        [15],
    ),
    (
        17,
        "request_deliverable_closure_verification agent dispatch",
        "Operator Verify & stamp button dispatches verifier agent with deliverable "
        "context, gate list, and closure prompt template.",
        "m3-operator",
        [16],
    ),
    (
        18,
        "Mission Page: Verify & stamp closure button + grade display",
        "New header action distinct from Record outcome; show latest grade, report "
        "summary, per-check pass/fail; link to full report.",
        "m3-operator",
        [16, 17],
    ),
    (
        19,
        "Block status=done without pass/waiver closure grade",
        "create_deliverable upsert rejects done unless latest closure grade is pass "
        "or waive; document waiver path for cancelled tasks.",
        "m3-operator",
        [15, 18],
    ),
    (
        20,
        "Dogfood: closure gate on mcp-agent-path-performance",
        "Register harness for perf deliverable; retroactive acceptance criteria; "
        "run Verify & stamp; produce first real closure report.",
        "m4-dogfood",
        [14, 15, 16],
    ),
    (
        21,
        "test_deliverable_closure_gate.py + CI registration",
        "Fixture deliverable with fake harness; scope pass/fail; grade persistence; "
        "register in scripts/switchboard_ci.sh.",
        "m4-dogfood",
        [15, 16],
    ),
    (
        22,
        "Exit gate review: operator runbook for new deliverables",
        "Document operator steps; enable proof_requirements on all new deliverables; "
        "close DELIVERABLES-12…21; mark deliverable-closure-gate done.",
        "m4-dogfood",
        [18, 19, 20, 21],
    ),
]


def _tid(n: int) -> str:
    return f"{WS}-{n}"


def _task_title(n: int) -> str:
    for num, title, _desc, _ms, _deps in TASKS:
        if num == n:
            return title
    raise KeyError(n)


def _find_task_by_title(title: str) -> str | None:
    for t in store.list_tasks(project=PROJECT):
        if (t.get("title") or "").strip() == title.strip():
            return t.get("task_id")
    return None


def _ensure_tasks() -> dict[int, str]:
    """Return map task_number -> task_id (may differ on skewed local boards)."""
    number_to_id: dict[int, str] = {}
    for n, title, desc, _ms, deps in TASKS:
        tid = _find_task_by_title(title)
        dep_ids = []
        for d in deps:
            if d not in number_to_id:
                prior = _find_task_by_title(_task_title(d))
                if prior:
                    number_to_id[d] = prior
            dep_ids.append(number_to_id[d])
        if not tid:
            created = store.create_task(
                {
                    "workstream_id": WS,
                    "workstream_name": "Deliverables platform",
                    "title": title,
                    "description": desc,
                    "status": "Not Started",
                    "depends_on": dep_ids,
                    "phase": "Build",
                    "owner_org": "6th Element Labs",
                    "owner_person_or_role": "Deliverables / closure gate",
                },
                actor=ACTOR,
                project=PROJECT,
            )
            tid = (created or {}).get("task_id")
            if not tid:
                raise SystemExit(f"failed to create task for: {title}")
            print(f"  created {tid}: {title[:60]}…")
        else:
            store.update_task(
                tid,
                {"description": desc, "depends_on": dep_ids},
                actor=ACTOR,
                project=PROJECT,
            )
        number_to_id[n] = tid
    return number_to_id


def _ensure_deliverable() -> dict:
    existing = store.get_deliverable(DELIVERABLE_ID, project=PROJECT)
    if existing and not existing.get("error"):
        print(f"  deliverable {DELIVERABLE_ID} exists — reusing")
        return existing
    deliverable = store.create_deliverable(
        {
            "id": DELIVERABLE_ID,
            "title": "Deliverable closure gate — verify, grade, stamp",
            "status": "in_progress",
            "end_state": (
                "Operators press Verify & stamp closure on any deliverable; a verifier "
                "agent runs scope + functional gates against acceptance_criteria and "
                "proof_requirements, records a graded closure report, and blocks "
                "status=done until pass or waiver."
            ),
            "why_it_matters": (
                "Task Done is not deliverable Done. This productizes proof that the whole "
                "shipped against the goals set at creation — the commercial unit Switchboard "
                "sells (verified progress)."
            ),
            "confidence": 0.85,
            "acceptance_criteria": [
                "New deliverables require acceptance_criteria + proof_requirements at in_progress",
                "Verify & stamp closure dispatches verifier agent and persists graded report",
                "status=done rejected without pass/waiver closure grade",
                "mcp-agent-path-performance dogfood produces first real closure report",
                "test_deliverable_closure_gate.py green in CI",
            ],
            "proof_requirements": {
                "schema": "switchboard.deliverable_proof_requirements.v1",
                "gates": [
                    {"id": "scope", "required": True},
                    {"id": "harness:test_deliverable_closure_gate", "required": True},
                ],
            },
        },
        actor=ACTOR,
        project=PROJECT,
    )
    print(f"  created deliverable {DELIVERABLE_ID}")
    return deliverable


def _ensure_milestones(deliverable: dict) -> dict[str, str]:
    by_title = {m.get("title"): m.get("id") for m in (deliverable.get("milestones") or [])}
    ids: dict[str, str] = {}
    for key, title, sort_order in MILESTONES:
        if title in by_title:
            ids[key] = by_title[title]
            continue
        res = store.add_deliverable_milestone(
            DELIVERABLE_ID,
            {"title": title, "status": "in_progress", "sort_order": sort_order},
            actor=ACTOR,
            project=PROJECT,
        )
        for m in res.get("milestones") or []:
            if m.get("title") == title:
                ids[key] = m["id"]
                break
        print(f"  milestone: {title}")
    return ids


def _link_tasks(deliverable: dict, milestone_ids: dict[str, str],
                number_to_id: dict[int, str]) -> None:
    linked = {l.get("task_id") for l in (deliverable.get("task_links") or [])}
    count = 0
    for n, _title, _desc, ms_key, _deps in TASKS:
        tid = number_to_id[n]
        if tid in linked:
            continue
        ms_id = milestone_ids.get(ms_key)
        store.link_task_to_deliverable(
            DELIVERABLE_ID,
            PROJECT,
            tid,
            milestone_id=ms_id,
            actor=ACTOR,
            project=PROJECT,
        )
        count += 1
    print(f"  linked {count} new tasks -> {DELIVERABLE_ID}")


def main() -> None:
    store.init_project_registry()
    if not store.has_project(PROJECT):
        raise SystemExit(f"project {PROJECT!r} does not exist")
    number_to_id = _ensure_tasks()
    deliverable = _ensure_deliverable()
    milestone_ids = _ensure_milestones(deliverable)
    deliverable = store.get_deliverable(DELIVERABLE_ID, project=PROJECT) or deliverable
    _link_tasks(deliverable, milestone_ids, number_to_id)
    print("== Done ==")
    print(f"View: ?project={PROJECT}&deliverable={DELIVERABLE_ID}#tab-mission")
    print("Spec: docs/DELIVERABLE-CLOSURE-GATE.md")


if __name__ == "__main__":
    main()
