#!/usr/bin/env python3
"""BUG-30 — keep MCP search_tasks on the slim, batched read path.

The production regression was six parallel lane searches on a ~275-task board.
This fixture rounds that up to 300 tasks and fails if agent search/summary code
touches the full per-task enrichment loader or misses the 150 ms read SLO.
"""
import concurrent.futures
import math
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="search-tasks-perf-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import store  # noqa: E402

PROJECT = "switchboard"
TASKS = 300
LANES = 12
PARALLEL_SEARCHES = 6
SEARCH_P99_BUDGET_MS = 150.0
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def percentile(values, rank):
    ordered = sorted(values)
    return ordered[max(0, math.ceil((rank / 100.0) * len(ordered)) - 1)]


try:
    store.init_db(PROJECT)
    for index in range(TASKS):
        lane = index % LANES
        store.create_task(
            {
                "workstream_id": f"LOAD{lane}",
                "title": f"Needle task {index}",
                "description": "BUG-30 300-task concurrent search fixture",
                "owner_person_or_role": f"Owner {index % 4}",
                "is_blocking": index % 10 == 0,
            },
            actor="test/setup",
            project=PROJECT,
        )

    original_fat_loader = store.list_tasks

    def forbidden_fat_loader(*args, **kwargs):
        raise AssertionError("agent list path called fat store.list_tasks")

    store.list_tasks = forbidden_fat_loader
    try:
        def search(lane):
            began = time.perf_counter()
            rows = agent._search_tasks(
                {"workstream": lane, "query": "needle"}, project=PROJECT)
            return rows, (time.perf_counter() - began) * 1_000.0

        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_SEARCHES) as executor:
            results = list(executor.map(search, [f"LOAD{i}" for i in range(PARALLEL_SEARCHES)]))

        latencies = [elapsed for _, elapsed in results]
        p99_ms = percentile(latencies, 99)
        ok(all(len(rows) == TASKS // LANES for rows, _ in results),
           "six filtered searches each return the expected 25 task briefs")
        ok(p99_ms < SEARCH_P99_BUDGET_MS,
           f"six-search p99 {p99_ms:.1f}ms < {SEARCH_P99_BUDGET_MS:.0f}ms on {TASKS} tasks")
        ok(all("session_health" not in row and "external_ci" not in row
               and "publication" not in row
               for rows, _ in results for row in rows),
           "search briefs omit detail-only enrichment")

        began = time.perf_counter()
        all_matches = agent._search_tasks({"query": "needle"}, project=PROJECT)
        unfiltered_ms = (time.perf_counter() - began) * 1_000.0
        ok(len(all_matches) == 60,
           "whole-board free-text search preserves the 60-result response cap")
        ok(unfiltered_ms < SEARCH_P99_BUDGET_MS,
           f"whole-board slim search {unfiltered_ms:.1f}ms < {SEARCH_P99_BUDGET_MS:.0f}ms")

        summary = agent.board_summary_text(project=PROJECT)
        ok("LOAD0-" in summary and "LOAD11-" in summary,
           "agent board summary also avoids the forbidden fat loader")
        rollups = store.board_rollups(project=PROJECT)
        ok(rollups["total_tasks"] == TASKS,
           "board rollups also use the slim loader without changing counts")
    finally:
        store.list_tasks = original_fat_loader
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nsearch_tasks perf: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
