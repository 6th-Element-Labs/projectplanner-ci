#!/usr/bin/env python3
"""HARDEN-36 — non-blocking hot reads + short-TTL cache.

The single uvicorn worker serves the live dashboard's polled reads. When those
handlers ran SQLite on the event loop (`async def` with a sync store call), one
slow query stalled every unrelated request — the same single-worker + SQLite
contention that surfaced as the CI "database is locked". #159 fixed only
/api/board. This guards the rest of the hot polled reads:

  1. THREADPOOL — /api/board, /api/signals, /api/mission_status and the
     deliverable mission_status/dependency_graph handlers are plain `def`, so
     Starlette runs them in the threadpool and a slow one can't block the loop.
  2. NON-SERIALIZING — proven over a real ASGI transport: while a deliberately
     slow /api/signals builds, concurrent /health calls still return promptly and
     two slow reads overlap instead of serializing.
  3. SHORT-TTL CACHE — a warmed hot read is served from store._READ_CACHE in
     <20ms, rebuilds at most once per stamp, and invalidates immediately on a
     write — INCLUDING a write to a linked task in ANOTHER project (the mission
     views fan out cross-project, so the cache stamp must too).
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="hotcache-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
import signals  # noqa: E402
import read_cache  # noqa: E402  (hot-read cache extracted from store; store re-exports it)

try:
    import httpx  # noqa: E402
    from httpx import ASGITransport  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  hot-read cache proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

HOME = "hot-home"      # deliverable owner / board project
OTHER = "hot-other"    # a *different* project a linked task lives in
passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


# --- fixture: two projects, tasks, a deliverable linking a task from each -------
store.init_project_registry()
store.create_project("Hot Home", project_id=HOME, actor="test")
store.init_db(HOME)
store.create_project("Hot Other", project_id=OTHER, actor="test")
store.init_db(OTHER)

home_task = store.create_task({"workstream_id": "HOT", "title": "Home task",
                               "finish_date": "2020-01-01"}, actor="test", project=HOME)
other_task = store.create_task({"workstream_id": "REMOTE", "title": "Remote task"},
                               actor="test", project=OTHER)
HOME_TID = home_task["task_id"]
OTHER_TID = other_task["task_id"]

deliv = store.create_deliverable({"title": "Hot Deliverable", "end_state": "ship it"},
                                 actor="test", project=HOME)
DID = deliv.get("id") or deliv.get("deliverable_id")
store.link_task_to_deliverable(DID, HOME, HOME_TID, actor="test", project=HOME)
store.link_task_to_deliverable(DID, OTHER, OTHER_TID, actor="test", project=HOME)


def count_calls(module, name):
    """Wrap module.<name> with an invocation counter; return (restore, counter)."""
    original = getattr(module, name)
    state = {"n": 0}

    def wrapper(*a, **k):
        state["n"] += 1
        return original(*a, **k)

    setattr(module, name, wrapper)
    return (lambda: setattr(module, name, original)), state


try:
    # === 1) THREADPOOL CONTRACT: the hot read handlers are `def`, not coroutines ===
    hot_paths = {
        "/api/board", "/api/signals", "/api/mission_status",
        "/api/deliverables/{deliverable_id}/mission_status",
        "/api/deliverables/{deliverable_id}/dependency_graph",
    }

    def iter_api_routes(routes):
        """Walk top-level and FastAPI 0.139+ `_IncludedRouter` nests (ARCH-MS-65)."""
        for route in routes:
            if getattr(route, "endpoint", None) is not None and getattr(route, "path", None):
                yield route
            nested = getattr(route, "routes", None)
            if nested:
                yield from iter_api_routes(nested)
            original = getattr(route, "original_router", None)
            if original is not None and getattr(original, "routes", None):
                yield from iter_api_routes(original.routes)

    seen = {}
    for route in iter_api_routes(app.routes):
        p = getattr(route, "path", None)
        if p in hot_paths and "GET" in (getattr(route, "methods", None) or set()):
            seen[p] = not asyncio.iscoroutinefunction(route.endpoint)
    for p in sorted(hot_paths):
        ok(seen.get(p) is True, f"{p} is threadpooled (sync def, runs off the event loop)")

    # === 2) NON-SERIALIZING over a real ASGI transport ==========================
    # Make the signals build deliberately slow; a threadpooled handler must let the
    # loop keep serving /health, and two slow reads must overlap (not serialize).
    SLOW = 0.5
    restore_slow, _ = None, None
    _real_compute = signals._compute_plan_signals

    def _slow_compute(*a, **k):
        time.sleep(SLOW)
        return _real_compute(*a, **k)

    signals._compute_plan_signals = _slow_compute
    store._READ_CACHE.clear()  # force cache misses so both slow builds actually run
    try:
        async def race():
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                async def timed(coro_factory):
                    t0 = time.time()
                    r = await coro_factory()
                    return r, (time.time() - t0)

                started = time.time()
                results = await asyncio.gather(
                    timed(lambda: c.get("/api/signals", params={"project": HOME})),
                    timed(lambda: c.get("/api/signals", params={"project": OTHER})),
                    timed(lambda: c.get("/health")),
                    timed(lambda: c.get("/health")),
                    timed(lambda: c.get("/health")),
                )
                wall = time.time() - started
                return results, wall

        (results, wall) = asyncio.run(race())
        sig_home, sig_other = results[0], results[1]
        healths = results[2:]
        ok(sig_home[0].status_code == 200 and sig_other[0].status_code == 200,
           "both slow /api/signals return 200")
        ok(all(h[0].status_code == 200 for h in healths), "concurrent /health calls return 200")
        max_health = max(h[1] for h in healths)
        ok(max_health < SLOW * 0.5,
           f"/health not stalled behind the slow read ({max_health*1000:.0f}ms << {SLOW*1000:.0f}ms build)")
        ok(wall < SLOW * 1.8,
           f"two slow reads overlapped in the threadpool (wall {wall*1000:.0f}ms < {SLOW*1.8*1000:.0f}ms serial bound)")
    finally:
        signals._compute_plan_signals = _real_compute

    # === 3) SHORT-TTL CACHE: hit <20ms, rebuild-once, write invalidates =========
    def warm_hit_ms(fn):
        fn()                       # miss (populate)
        t0 = time.time()
        fn()                       # hit
        return (time.time() - t0) * 1000

    store._READ_CACHE.clear()
    ok(warm_hit_ms(lambda: store.board_payload(HOME, lite=True)) < 20, "board cache hit < 20ms")
    ok(warm_hit_ms(lambda: signals.compute_plan_signals(project=HOME)) < 20, "signals cache hit < 20ms")
    ok(warm_hit_ms(lambda: store.get_mission_status(project=HOME, deliverable_id=DID)) < 20,
       "mission_status cache hit < 20ms")
    ok(warm_hit_ms(lambda: store.get_deliverable_dependency_graph(project=HOME, deliverable_id=DID)) < 20,
       "dependency_graph cache hit < 20ms")

    # rebuild-at-most-once-per-stamp + same-project invalidation (board & signals)
    store._READ_CACHE.clear()
    restore, cnt = count_calls(store, "_build_board_payload")
    try:
        store.board_payload(HOME, lite=True)
        store.board_payload(HOME, lite=True)
        ok(cnt["n"] == 1, "board built once for a burst of reads (served from cache)")
        store.update_task(HOME_TID, {"title": "Home task edited"}, actor="test", project=HOME)
        store.board_payload(HOME, lite=True)
        ok(cnt["n"] == 2, "a task write invalidates the board cache immediately")
    finally:
        restore()

    store._READ_CACHE.clear()
    restore, cnt = count_calls(signals, "_compute_plan_signals")
    try:
        signals.compute_plan_signals(project=HOME)
        signals.compute_plan_signals(project=HOME)
        ok(cnt["n"] == 1, "signals computed once for a burst of reads (served from cache)")
        store.update_task(HOME_TID, {"status": "In Progress"}, actor="test", project=HOME)
        signals.compute_plan_signals(project=HOME)
        ok(cnt["n"] == 2, "a task write invalidates the signals cache immediately")
    finally:
        restore()

    # CROSS-PROJECT invalidation: mission_status / dependency_graph fan out into the
    # linked task's OWN project, so a write there must bump the stamp and rebuild.
    store._READ_CACHE.clear()
    restore, cnt = count_calls(store, "_build_mission_status")
    try:
        store.get_mission_status(project=HOME, deliverable_id=DID)
        store.get_mission_status(project=HOME, deliverable_id=DID)
        ok(cnt["n"] == 1, "mission_status built once for a burst of reads (served from cache)")
        store.update_task(OTHER_TID, {"status": "In Progress"}, actor="test", project=OTHER)
        store.get_mission_status(project=HOME, deliverable_id=DID)
        ok(cnt["n"] == 2, "a linked-task write in ANOTHER project invalidates mission_status")
    finally:
        restore()

    store._READ_CACHE.clear()
    restore, cnt = count_calls(store, "_build_deliverable_dependency_graph")
    try:
        store.get_deliverable_dependency_graph(project=HOME, deliverable_id=DID)
        store.get_deliverable_dependency_graph(project=HOME, deliverable_id=DID)
        ok(cnt["n"] == 1, "dependency_graph built once for a burst of reads (served from cache)")
        store.update_task(OTHER_TID, {"title": "Remote task edited"}, actor="test", project=OTHER)
        store.get_deliverable_dependency_graph(project=HOME, deliverable_id=DID)
        ok(cnt["n"] == 2, "a linked-task write in ANOTHER project invalidates dependency_graph")
    finally:
        restore()

    # === 4) SHARED-OBJECT CONTRACT: a consumer that trims a COPY (as agent.py's
    # plan_signals tool does) must not shrink the shared cached lists ============
    store._READ_CACHE.clear()
    first = signals.compute_plan_signals(project=HOME)
    overdue_len = len(first["overdue"])
    ok(overdue_len >= 1, "signals has an overdue task to guard against truncation")
    consumer = dict(signals.compute_plan_signals(project=HOME))   # agent.py's copy-first pattern
    for k in ("overdue", "due_soon", "blocked", "ready", "critical_slip"):
        consumer[k] = consumer[k][:0]
    again = signals.compute_plan_signals(project=HOME)
    ok(len(again["overdue"]) == overdue_len,
       "trimming a copy of cached signals does not corrupt the shared cached lists")

    # === 5) CACHE CORRECTNESS: a cached hit equals a fresh build =================
    store._READ_CACHE.clear()
    fresh = store.get_mission_status(project=HOME, deliverable_id=DID)
    cached = store.get_mission_status(project=HOME, deliverable_id=DID)
    ok(fresh == cached, "cached mission_status equals a freshly built one")

    # === 6) SERVE-STALE-WHILE-REVALIDATE: an expired-but-unchanged-stamp hit is served
    # instantly and refreshed in the background; a CHANGED stamp still rebuilds inline ===
    store._READ_CACHE.clear()
    n = {"v": 0}
    def slow_builder():
        n["v"] += 1
        return {"v": n["v"]}

    r1 = store.ttl_read_cache("t6", "id", "STAMP-A", slow_builder, ttl=0.05)
    ok(n["v"] == 1 and r1 == {"v": 1}, "serve-stale: cold miss builds synchronously")
    r2 = store.ttl_read_cache("t6", "id", "STAMP-A", slow_builder, ttl=0.05)
    ok(n["v"] == 1 and r2 == {"v": 1}, "serve-stale: fresh hit served without a rebuild")

    time.sleep(0.06)  # lapse the TTL, stamp unchanged
    r3 = store.ttl_read_cache("t6", "id", "STAMP-A", slow_builder, ttl=0.05)
    ok(r3 == {"v": 1}, "serve-stale: expired+same-stamp returns the STALE payload instantly")
    deadline = time.time() + 2.0
    while n["v"] < 2 and time.time() < deadline:
        time.sleep(0.02)
    ok(n["v"] == 2, "serve-stale: exactly one background refresh rebuilt the entry")
    r4 = store.ttl_read_cache("t6", "id", "STAMP-A", slow_builder, ttl=100)
    ok(r4 == {"v": 2} and n["v"] == 2, "serve-stale: the refreshed payload is now served")

    r5 = store.ttl_read_cache("t6", "id", "STAMP-B", slow_builder, ttl=100)
    ok(r5 == {"v": 3} and n["v"] == 3, "serve-stale: a CHANGED stamp rebuilds synchronously (never serves stale-wrong)")

    store._READ_CACHE.clear()
    n["v"] = 0
    saved = read_cache._READ_CACHE_STALE_REVALIDATE
    read_cache._READ_CACHE_STALE_REVALIDATE = False
    try:
        store.ttl_read_cache("t6b", "id", "S", slow_builder, ttl=0.05)  # cold → 1
        time.sleep(0.06)
        r6 = store.ttl_read_cache("t6b", "id", "S", slow_builder, ttl=0.05)  # expired → inline rebuild
        ok(r6 == {"v": 2} and n["v"] == 2, "serve-stale kill switch: expiry rebuilds synchronously when disabled")
    finally:
        read_cache._READ_CACHE_STALE_REVALIDATE = saved
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nhot-read cache: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
