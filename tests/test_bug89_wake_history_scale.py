#!/usr/bin/env python3
"""BUG-89: wake polling stays bounded as terminal history grows."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from path_setup import ROOT


TMP = Path(tempfile.mkdtemp(prefix="bug89-wake-history-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from app import app  # noqa: E402
from db.connection import _conn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from switchboard.application.queries.audit_export import execute as audit_export  # noqa: E402


PROJECT = "switchboard"
BASE = 2_000_000_000.0
HISTORY_ROWS = 20_000
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(PROJECT)
    terminal_rows = []
    statuses = ("completed", "failed", "cancelled")
    for i in range(HISTORY_ROWS):
        selector = {"runtime": "codex" if i % 2 == 0 else "cursor",
                    "deliverable_id": "deliverable-a" if i % 3 == 0 else "deliverable-b"}
        terminal_rows.append((
            f"wake-history-{i:05d}", "bug89", "scale proof",
            json.dumps(selector), "{}", statuses[i % len(statuses)], BASE + i,
            BASE + i + 1, "{}", "{}", f"OLD-{i:05d}",
        ))
    active_rows = [
        ("wake-active-pending", "bug89", "active proof",
         json.dumps({"runtime": "codex", "deliverable_id": "deliverable-a"}),
         "{}", "pending", BASE + HISTORY_ROWS + 1, None, "{}", "{}", "LIVE-1"),
        ("wake-active-claimed", "bug89", "active proof",
         json.dumps({"runtime": "codex", "deliverable_id": "deliverable-a"}),
         "{}", "claimed", BASE + HISTORY_ROWS + 2, None, "{}", "{}", "LIVE-2"),
    ]
    with _conn(PROJECT) as c:
        c.executemany(
            "INSERT INTO wake_intents(wake_id,source,reason,selector_json,policy_json,"
            "status,requested_at,completed_at,result_json,placement_json,task_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            terminal_rows + active_rows,
        )

    started = time.perf_counter()
    active = store.list_wake_intents(
        project=PROJECT, active_only=True, newest_first=True, limit=50)
    elapsed = time.perf_counter() - started
    ok([w["wake_id"] for w in active]
       == ["wake-active-claimed", "wake-active-pending"],
       "ordinary polling returns only active wakes, newest first")
    ok(elapsed < 0.25,
       f"20k-row active polling is bounded ({elapsed:.4f}s < 0.25s)")

    client = TestClient(app)
    active_response = client.get(
        f"/ixp/v1/wake_intents?project={PROJECT}&limit=50")
    active_payload = active_response.json()
    ok(active_response.status_code == 200
       and len(active_payload["wake_intents"]) == 2
       and active_payload["page"]["has_more"] is False,
       "Fleet REST defaults to a bounded active-only page")
    history_response = client.get(
        f"/ixp/v1/wake_intents?project={PROJECT}&history=true&limit=8")
    history_payload = history_response.json()
    ok(history_response.status_code == 200
       and len(history_payload["wake_intents"]) == 8
       and history_payload["page"]["has_more"] is True
       and history_payload["page"]["next_before_wake_id"],
       "explicit REST history returns a bounded page and next cursor")
    ok(client.get(
        f"/ixp/v1/wake_intents?project={PROJECT}&history=true&limit=201"
    ).status_code == 422,
       "REST rejects page sizes above the 200-row server ceiling")

    first = store.list_wake_intents(
        project=PROJECT, include_archived=True, newest_first=True, limit=7)
    last = first[-1]
    second = store.list_wake_intents(
        project=PROJECT, include_archived=True, newest_first=True, limit=7,
        before_requested_at=last["requested_at"], before_wake_id=last["wake_id"])
    ok(len(first) == 7 and len(second) == 7
       and not ({w["wake_id"] for w in first} & {w["wake_id"] for w in second})
       and second[0]["requested_at"] < first[-1]["requested_at"],
       "history uses stable, non-overlapping keyset pages")

    filtered = store.list_wake_intents(
        project=PROJECT, runtime="cursor", deliverable_id="deliverable-b",
        include_archived=True, newest_first=True, limit=11)
    ok(len(filtered) == 11 and all(
        w["selector"].get("runtime") == "cursor"
        and w["selector"].get("deliverable_id") == "deliverable-b"
        for w in filtered),
       "runtime and deliverable filters execute before the SQL limit")

    with _conn(PROJECT) as c:
        plan = " ".join(str(value) for row in c.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM wake_intents "
            "WHERE archived_at IS NULL AND status IN ('pending','claimed') "
            "ORDER BY requested_at DESC, wake_id DESC LIMIT 50"
        ).fetchall() for value in row).lower()
    ok("ix_wake_intents_live_recent" in plan,
       "SQLite uses the live/recent wake index instead of scanning history")
    with _conn(PROJECT) as c:
        history_plan = " ".join(str(value) for row in c.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM wake_intents "
            "ORDER BY requested_at DESC, wake_id DESC LIMIT 8"
        ).fetchall() for value in row).lower()
    ok("ix_wake_intents_recent" in history_plan,
       "recent-history fallback reads the newest index entries without a full sort")

    dry = store.apply_cleanup(
        project=PROJECT, dry_run=True, proof_task_age_days=0,
        include_kinds=["wake_intent_history"], now=BASE + HISTORY_ROWS + 100)
    ok(dry["summary"]["total"] == 500
       and all(c["kind"] == "wake_intent_history" for c in dry["candidates"]),
       "terminal cleanup is capped at a 500-row batch")
    ok(not any(c.get("task_id") in {"LIVE-1", "LIVE-2"} for c in dry["candidates"]),
       "active wakes are never terminal-history cleanup candidates")

    selected = [c["id"] for c in dry["candidates"][:3]]
    applied = store.apply_cleanup(
        project=PROJECT, candidate_ids=selected, dry_run=False,
        proof_task_age_days=0, include_kinds=["wake_intent_history"],
        actor="codex/BUG-89", reason="bounded history cleanup proof",
        now=BASE + HISTORY_ROWS + 100)
    ok(applied["applied_count"] == 3,
       "selected terminal wakes archive through the audited cleanup path")
    archived_ids = {c.split(":", 1)[1] for c in selected}
    ordinary_ids = {w["wake_id"] for w in store.list_wake_intents(project=PROJECT)}
    history_ids = {w["wake_id"] for w in store.list_wake_intents(
        project=PROJECT, include_archived=True)}
    ok(not (archived_ids & ordinary_ids) and archived_ids <= history_ids,
       "archived wakes leave ordinary reads but remain explicitly retrievable")

    exported_ids = {w["wake_id"] for w in audit_export(project=PROJECT)["wake_intents"]}
    ok(archived_ids <= exported_ids,
       "archiving preserves terminal wakes in the audit export")
    with _conn(PROJECT) as c:
        audit_count = c.execute(
            "SELECT COUNT(*) n FROM activity WHERE kind='cleanup.wake_archived'"
        ).fetchone()["n"]
    ok(audit_count == 3, "each archived wake writes cleanup.wake_archived evidence")

    app_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    api_source = (ROOT / "src" / "switchboard" / "api" / "routers" / "wakes.py").read_text(
        encoding="utf-8")
    ok("&limit=100" in app_source and "&history=true&limit=8" in app_source,
       "Fleet requests active wakes first and only eight history rows as fallback")
    ok("limit: int = Query(50, ge=1, le=200)" in api_source,
       "TXP and IXP enforce a server-side page-size ceiling")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
