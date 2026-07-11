"""Process-wide non-blocking concurrency gate for expensive HTTP/MCP work (PERF-5).

Little's Law on a 2-vCPU box: unbounded concurrent expensive ops queue behind SQLite
and thread pools until clients see 504s.  This limiter rejects *new* work immediately
with 429 + ``Retry-After`` when slots are full — graceful shedding beats accepting
work the box cannot finish.  Pairs with the durable webhook inbox: the
``/api/github/webhook`` accept-and-ack path is never gated here.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, FrozenSet, Optional, Tuple

CONCURRENCY_EXEMPT_PATHS: FrozenSet[str] = frozenset({
    "/api/github/webhook",
    "/health",
    "/health/saturation",
    "/health/deep",
})

_lock = threading.Lock()
_inflight = 0
_shed_total = 0
_test_limit: Optional[int] = None


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def configured_limit() -> int:
    if _test_limit is not None:
        return _test_limit
    return _int_env("PM_GLOBAL_EXPENSIVE_CONCURRENCY", 4)


def retry_after_s() -> int:
    return _int_env("PM_CONCURRENCY_RETRY_AFTER_S", 2)


def enabled() -> bool:
    value = (os.environ.get("PM_GLOBAL_CONCURRENCY_ENABLED") or "1").strip().lower()
    return value in {"1", "true", "on", "yes"}


def is_exempt_path(path: str) -> bool:
    return (path or "") in CONCURRENCY_EXEMPT_PATHS


def is_expensive_request(method: str, path: str) -> bool:
    """Return True when the request should consume a global expensive-op slot."""
    if not enabled():
        return False
    if is_exempt_path(path):
        return False
    return (method or "").upper() not in {"GET", "HEAD", "OPTIONS"}


def _snapshot_unlocked(*, limit: Optional[int] = None) -> dict:
    resolved = limit if limit is not None else configured_limit()
    available = max(0, resolved - _inflight)
    return {
        "schema": "switchboard.concurrency_limiter.v1",
        "enabled": enabled(),
        "limit": resolved,
        "inflight": _inflight,
        "available": available,
        "saturated": _inflight >= resolved,
        "retry_after_s": retry_after_s(),
        "shed_total": _shed_total,
    }


def snapshot(*, limit: Optional[int] = None) -> dict:
    with _lock:
        return _snapshot_unlocked(limit=limit)


def try_acquire() -> Tuple[bool, dict]:
    """Non-blocking slot acquire.  Returns (acquired, snapshot)."""
    global _inflight, _shed_total
    with _lock:
        limit = configured_limit()
        if _inflight >= limit:
            _shed_total += 1
            snap = _snapshot_unlocked(limit=limit)
            snap["shed"] = True
            return False, snap
        _inflight += 1
        snap = _snapshot_unlocked(limit=limit)
        snap["shed"] = False
        return True, snap


def release() -> None:
    global _inflight
    with _lock:
        _inflight = max(0, _inflight - 1)


def reset_for_tests(*, limit: Optional[int] = None, inflight: int = 0, shed_total: int = 0) -> None:
    """Hermetic tests only — restore process-local limiter state."""
    global _inflight, _shed_total, _test_limit
    with _lock:
        _test_limit = limit
        _inflight = max(0, int(inflight or 0))
        _shed_total = max(0, int(shed_total or 0))


def build_shed_payload(snap: Optional[dict] = None) -> dict:
    snap = snap or snapshot()
    return {
        "error": "concurrency_limit",
        "schema": "switchboard.concurrency_limit.v1",
        "inflight": snap.get("inflight", 0),
        "limit": snap.get("limit", configured_limit()),
        "retry_after_s": int(snap.get("retry_after_s") or retry_after_s()),
    }


def build_shed_headers(snap: Optional[dict] = None) -> dict:
    snap = snap or snapshot()
    return {"Retry-After": str(int(snap.get("retry_after_s") or retry_after_s()))}


class ConcurrencyLimitASGIMiddleware:
    """ASGI backpressure for the standalone MCP HTTP server (PERF-5)."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        method = (scope.get("method") or b"GET")
        if isinstance(method, bytes):
            method = method.decode("latin-1")
        path = scope.get("path") or ""
        if not is_expensive_request(method, path):
            return await self.app(scope, receive, send)

        acquired, snap = try_acquire()
        if not acquired:
            body = json.dumps(build_shed_payload(snap), sort_keys=True).encode("utf-8")
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"retry-after", build_shed_headers(snap)["Retry-After"].encode("ascii")),
            ]
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        released = False

        def _release_once() -> None:
            nonlocal released
            if not released:
                released = True
                release()

        async def send_with_release(message):
            if message.get("type") == "http.response.start":
                _release_once()
            await send(message)

        try:
            await self.app(scope, receive, send_with_release)
        except Exception:
            _release_once()
            raise
        finally:
            _release_once()
