#!/usr/bin/env python3
"""HARDEN-41 / HARDEN-35 — board load performance budget.

Guards the board's hot path against regressing to the whole-board N+1 + fat
payload that jammed the box: the board must use the slim, batched loader (no
per-task session_health/external_ci/publication enrichment), stay under a
wire-size budget, support conditional (ETag/304) reloads, and — per HARDEN-35 —
keep the ~9KB project_context blob and detail-only prose out of the payload,
landing >=50% smaller than the pre-slim baseline.
"""
import json
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="board-perf-")
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
    print(f"  SKIP  board perf proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

HOME = "perf-home"
N = 200
passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


store.init_project_registry()
store.create_project("Perf Home", project_id=HOME, actor="test")
store.init_db(HOME)
for i in range(N):
    store.create_task({"workstream_id": "PERF", "title": f"Task {i}",
                       "description": "d" * 200, "exit_criteria": "e" * 200},
                      actor="test", project=HOME)

client = TestClient(app)
try:
    # 1) batched slim loader drops the 3 heavy per-task enrichments (the N+1)
    board_rows = store.list_tasks_for_board(HOME)
    ok(len(board_rows) == N, f"board loader returns all {N} tasks")
    ok(all("provenance" in t for t in board_rows), "board loader keeps provenance")
    heavy = ("session_health", "external_ci", "publication")
    ok(not any(k in t for t in board_rows for k in heavy),
       "board loader omits session_health/external_ci/publication (no per-task N+1)")
    ok(all(k in store.list_tasks(project=HOME)[0] for k in heavy),
       "full list_tasks still enriches (get_task path unchanged)")

    # 2) wire-size budget on /api/board (audit target: < 400KB for the seed)
    r = client.get("/api/board", params={"project": HOME})
    ok(r.status_code == 200, "GET /api/board 200")
    size = len(r.content)
    budget = 400 * 1024
    ok(size < budget, f"/api/board {size // 1024}KB < {budget // 1024}KB budget for {N} tasks")
    body = r.json()
    ts = [t for ws in body["workstreams"] for t in ws["tasks"]]
    ok(len(ts) == N and "rollups" in body, "board payload structurally intact")

    # HARDEN-35: project_context (a fixed ~9KB blob) rides on its own endpoint
    # now, not the board; and detail-only prose stays out of the board rows.
    ok("project_context" not in body,
       "/api/board no longer bundles project_context (split to /context)")
    detail_only = ("deliverable", "entry_criteria", "exit_criteria")
    ok(not any(k in t for t in ts for k in detail_only),
       "board task rows omit deliverable/entry_criteria/exit_criteria (task-detail only)")
    ok(all("description" in t for t in ts),
       "board rows keep description (the board text search matches against it)")
    ctx = client.get("/api/projects/{}/context".format(HOME))
    ok(ctx.status_code == 200 and (ctx.json().get("repo_role_guide") is not None),
       "/api/projects/{id}/context serves the project_context the board used to bundle")

    # HARDEN-35 acceptance: the slim board is >=50% smaller than the pre-slim
    # baseline (full per-task enrichment + the bundled project_context blob).
    fat_tasks = store.list_tasks(project=HOME)
    fat_payload = {"workstreams": [{"tasks": fat_tasks}],
                   "project_context": store.get_project_context(HOME)}
    fat_size = len(json.dumps(fat_payload, default=str, separators=(",", ":")).encode())
    ok(size <= fat_size * 0.5,
       f"/api/board {size // 1024}KB is <=50% of the {fat_size // 1024}KB pre-slim baseline")

    # 3) conditional reload: ETag -> 304 with no body
    etag = r.headers.get("etag")
    ok(bool(etag), "board response carries an ETag")
    r304 = client.get("/api/board", params={"project": HOME}, headers={"If-None-Match": etag})
    ok(r304.status_code == 304 and len(r304.content) == 0, "matching If-None-Match -> 304, empty body")

    # 4) gross-regression timing bound (generous — not a microbenchmark)
    store._READ_CACHE.clear()  # HARDEN-36: board cache generalized into the shared read cache
    t0 = time.time()
    store.board_payload(HOME, lite=True)
    build_ms = (time.time() - t0) * 1000
    ok(build_ms < 1000, f"board_payload builds in {build_ms:.0f}ms (< 1000ms for {N} tasks)")
    t0 = time.time()
    store.board_payload(HOME, lite=True)
    cached_ms = (time.time() - t0) * 1000
    ok(cached_ms < 50, f"cached board read {cached_ms:.1f}ms (< 50ms)")

    # 5) deletion invalidates the board read-cache within the TTL window — a
    #    removed card can't linger. The victim is the oldest task (min updated_at),
    #    so a MAX(updated_at)-only stamp would miss the delete and serve a stale
    #    board; project_task_stamp folds in COUNT(*) to catch it (HARDEN-41).
    store._READ_CACHE.clear()  # HARDEN-36: board rides the shared read cache
    warmed = store.board_payload(HOME, lite=True)  # warms the cache
    n_before = sum(len(ws["tasks"]) for ws in warmed["workstreams"])
    victim = min(board_rows, key=lambda t: t.get("updated_at") or 0)["task_id"]
    ok(store.delete_task(victim, project=HOME), f"deleted oldest task {victim}")
    after = store.board_payload(HOME, lite=True)  # same TTL window, must rebuild
    n_after = sum(len(ws["tasks"]) for ws in after["workstreams"])
    ok(n_after == n_before - 1,
       f"deleted task drops off board immediately ({n_before} -> {n_after})")
    ok(all(t["task_id"] != victim for ws in after["workstreams"] for t in ws["tasks"]),
       "deleted task absent from cached board payload (no stale card)")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nboard load perf: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
