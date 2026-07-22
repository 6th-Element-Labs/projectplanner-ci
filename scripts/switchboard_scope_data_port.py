#!/usr/bin/env python3
"""Scope ADR-0018 on the board **via the Switchboard API** (no host/db access).

Creates the DATA-PORT workstream (DATA-PORT-1 … DATA-PORT-12 with the charter's
depends_on graph), deliverable `data-port-storage-abstraction`, its six charter
milestones, and task→milestone links — mirroring docs/DATA-PORT-EXECUTION.md —
using only the public REST surface (`POST /api/tasks`, `POST /api/deliverables`,
`POST /api/deliverables/{id}/milestones`, `POST /api/deliverables/{id}/task_links`).

Statuses are left to Switchboard provenance rules: everything seeds Not Started;
DATA-PORT-1 (the charter itself) flips via merge webhook/reconcile when the
ADR-0018 PR lands. No merge provenance is faked.

Idempotent: re-running reuses tasks/deliverable/milestones and only fills gaps.

Run:
    SWITCHBOARD_TOKEN=<bearer with write:tasks on project switchboard> \
        python scripts/switchboard_scope_data_port.py
    # SWITCHBOARD_URL defaults to https://plan.taikunai.com; override for local/dev.

View: ?project=switchboard&deliverable=data-port-storage-abstraction#tab-mission
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = (os.environ.get("SWITCHBOARD_URL") or "https://plan.taikunai.com").rstrip("/")
TOKEN = (os.environ.get("SWITCHBOARD_TOKEN") or "").strip()

PROJECT = "switchboard"
WS = "DATA-PORT"
WS_NAME = "Backend-agnostic storage ports"
DELIVERABLE_ID = "data-port-storage-abstraction"

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


def _client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    return httpx.Client(base_url=BASE, headers=headers, timeout=30.0,
                        params={"project": PROJECT})


def _die(what: str, r: httpx.Response) -> None:
    raise SystemExit(f"{what} failed: HTTP {r.status_code} — {r.text[:400]}")


def _existing_task_ids(c: httpx.Client) -> set:
    r = c.get("/api/tasks")
    if r.status_code != 200:
        _die("GET /api/tasks", r)
    body = r.json()
    tasks = body.get("tasks") if isinstance(body, dict) else body
    return {t.get("task_id") for t in (tasks or [])}


def _ensure_tasks(c: httpx.Client) -> None:
    existing = _existing_task_ids(c)
    for n, title, deps, _milestone in TASKS:
        tid = _tid(n)
        if tid in existing:
            continue
        r = c.post("/api/tasks", json={
            "workstream_id": WS, "workstream_name": WS_NAME,
            "title": title, "status": "Not Started",
            "depends_on": [_tid(d) for d in deps], "phase": "Build",
            "idem_key": f"data-port-scope:{tid}",
        })
        if r.status_code != 200:
            _die(f"POST /api/tasks ({tid})", r)
        got = r.json().get("task_id")
        if got != tid:
            raise SystemExit(
                f"expected {tid} but create yielded {got}; board already has "
                f"{WS} tasks with different numbering — clear them or adjust.")
        print(f"  created {tid}: {title}")
    print(f"  {WS}-1 … {WS}-{len(TASKS)} present (statuses follow provenance rules)")


def _get_deliverable(c: httpx.Client) -> dict | None:
    r = c.get(f"/api/deliverables/{DELIVERABLE_ID}")
    if r.status_code == 200:
        body = r.json()
        return body.get("deliverable") if "deliverable" in body else body
    return None


def _ensure_deliverable(c: httpx.Client) -> dict:
    existing = _get_deliverable(c)
    if existing:
        print(f"  deliverable {DELIVERABLE_ID} exists — reusing")
        return existing
    r = c.post("/api/deliverables", json={
        "id": DELIVERABLE_ID,
        "title": "Backend-agnostic storage ports (DATA-PORT)",
        "status": "in_progress", "end_state": END_STATE,
        "why_it_matters": "Operator requirement: the app must not care about the underlying "
                          "database — pick/choose any relational backend as an adapter "
                          "decision. Makes ARCH-19's eventual swap one adapter + gates "
                          "per bounded context instead of an app-wide migration.",
        "confidence": 0.75,
    })
    if r.status_code != 200:
        _die("POST /api/deliverables", r)
    print(f"  created deliverable {DELIVERABLE_ID}")
    return _get_deliverable(c) or {}


def _ensure_milestones(c: httpx.Client, deliverable: dict) -> dict:
    have = {m.get("title") for m in (deliverable.get("milestones") or [])}
    for title in MILESTONES:
        if title in have:
            continue
        status = "in_progress" if title == "charter-rails" else "not_started"
        r = c.post(f"/api/deliverables/{DELIVERABLE_ID}/milestones",
                   json={"title": title, "status": status})
        if r.status_code != 200:
            _die(f"POST milestones ({title})", r)
        print(f"  added milestone {title}")
    refreshed = _get_deliverable(c) or {}
    return {m.get("title"): m.get("id") for m in (refreshed.get("milestones") or [])}


def main() -> None:
    if not TOKEN and "plan.taikunai.com" in BASE:
        raise SystemExit("SWITCHBOARD_TOKEN required against the live host "
                         "(bearer with write:tasks on project switchboard)")
    with _client() as c:
        _ensure_tasks(c)
        deliverable = _ensure_deliverable(c)
        milestone_ids = _ensure_milestones(c, deliverable)

        linked = {l.get("task_id") for l in (deliverable.get("task_links") or [])}
        count = 0
        for n, _title, _deps, milestone in TASKS:
            tid = _tid(n)
            if tid in linked:
                continue
            r = c.post(f"/api/deliverables/{DELIVERABLE_ID}/task_links",
                       json={"task_id": tid, "task_project": PROJECT,
                             "milestone_id": milestone_ids[milestone]})
            if r.status_code != 200:
                _die(f"POST task_links ({tid})", r)
            count += 1
        print(f"  linked {count} tasks ({WS}-1 … {WS}-{len(TASKS)}) -> {DELIVERABLE_ID}")

        final = _get_deliverable(c) or {}
        print("== Done ==")
        print(f"  milestones: {[m.get('title') for m in final.get('milestones') or []]}")
        print(f"  linked tasks: {len(final.get('task_links') or [])}")
        print(f"View: {BASE}/?project={PROJECT}&deliverable={DELIVERABLE_ID}#tab-mission")


if __name__ == "__main__":
    main()
