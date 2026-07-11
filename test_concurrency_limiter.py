#!/usr/bin/env python3
"""PERF-5 — global concurrency limiter and backpressure tests."""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="concurrency-limiter-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_GLOBAL_CONCURRENCY_ENABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import concurrency_limiter  # noqa: E402
import saturation_signals  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


concurrency_limiter.reset_for_tests(limit=2, inflight=0, shed_total=0)
ok(concurrency_limiter.configured_limit() == 2, "test override sets configured limit")

acquired, snap = concurrency_limiter.try_acquire()
ok(acquired and snap["inflight"] == 1, "first acquire succeeds")
acquired2, _ = concurrency_limiter.try_acquire()
ok(acquired2, "second acquire succeeds at limit=2")
acquired3, shed_snap = concurrency_limiter.try_acquire()
ok(not acquired3 and shed_snap.get("shed") is True, "third acquire is rejected when saturated")
ok(shed_snap["shed_total"] == 1, "shed_total increments on rejection")
concurrency_limiter.release()
concurrency_limiter.release()
concurrency_limiter.release()
ok(concurrency_limiter.snapshot()["inflight"] == 0, "release drains inflight count")

ok(concurrency_limiter.is_exempt_path("/api/github/webhook"), "webhook path is exempt")
ok(not concurrency_limiter.is_expensive_request("POST", "/api/github/webhook"),
   "webhook POST is not expensive for limiter")
ok(not concurrency_limiter.is_expensive_request("GET", "/api/board"), "GET reads are exempt")
ok(concurrency_limiter.is_expensive_request("POST", "/api/tasks"), "task writes are expensive")

payload = concurrency_limiter.build_shed_payload({"inflight": 2, "limit": 2, "retry_after_s": 3})
ok(payload["schema"] == "switchboard.concurrency_limit.v1", "shed payload schema")
ok(concurrency_limiter.build_shed_headers(payload)["Retry-After"] == "3",
   "Retry-After header uses configured seconds")

store.init_db("switchboard")
sat = saturation_signals.compute_saturation_signals("switchboard")
ok("concurrency_limiter" in sat, "saturation snapshot includes concurrency_limiter")
ok(sat["concurrency_limiter"]["schema"] == "switchboard.concurrency_limiter.v1",
   "saturation concurrency block schema")

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  FastAPI middleware smoke requires optional dependency: {exc.name}")
else:
    concurrency_limiter.reset_for_tests(limit=1, inflight=0, shed_total=0)
    client = TestClient(app)

    ok(client.get("/health").status_code == 200, "health stays available under limiter")
    ok(client.get("/health/saturation", params={"project": "switchboard"}).status_code == 200,
       "saturation health stays available under limiter")

    concurrency_limiter.reset_for_tests(limit=1, inflight=1, shed_total=0)
    blocked = client.post("/api/tasks", json={"title": "x"}, params={"project": "switchboard"})
    ok(blocked.status_code == 429, "expensive POST gets 429 when limiter is saturated")
    ok(blocked.headers.get("retry-after") is not None, "429 includes Retry-After header")
    body = blocked.json()
    ok(body.get("error") == "concurrency_limit", "429 body names concurrency_limit")
    concurrency_limiter.reset_for_tests(limit=4, inflight=0, shed_total=0)

try:
    from starlette.testclient import TestClient as StarletteTestClient  # noqa: E402
except ModuleNotFoundError:
    print("  SKIP  MCP ASGI middleware smoke requires starlette TestClient")
else:
    from concurrency_limiter import ConcurrencyLimitASGIMiddleware  # noqa: E402

    async def _ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    concurrency_limiter.reset_for_tests(limit=1, inflight=1, shed_total=0)
    wrapped = ConcurrencyLimitASGIMiddleware(_ok_app)
    mcp_client = StarletteTestClient(wrapped)
    resp = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    ok(resp.status_code == 429, "MCP ASGI middleware returns 429 when saturated")
    ok(resp.headers.get("retry-after") is not None, "MCP 429 includes Retry-After")

shutil.rmtree(_TMP, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
