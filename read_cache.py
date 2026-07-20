"""Short-TTL in-memory read cache for the hot polled dashboard reads — the lite
board, plan signals, and the mission cockpit's status/dependency-graph views
(HARDEN-36, extending #159's board-only cache). These are the endpoints a live
dashboard hammers on a timer; every one runs SQLite in the request thread, so a
burst of viewers used to rebuild the same payload N times. Each entry is keyed by
a content STAMP (usually the involved project(s) MAX(updated_at)): any write bumps
the stamp and invalidates immediately, while the TTL bounds staleness for the few
signals a stamp can't see (claim expiry, presence heartbeats, rarely-changing
board meta) to at most _READ_CACHE_TTL.

Extracted from store.py per ADR-0006 (the hot-read cache is a self-contained leaf
mechanism — it only runs a builder callback, so it has no store dependency). store
re-exports `ttl_read_cache` / `_READ_CACHE` for its callers and back-compat.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

__all__ = ["_READ_CACHE", "ttl_read_cache"]

_READ_CACHE: Dict[str, Dict[str, Any]] = {}
_READ_CACHE_TTL = float(os.environ.get("PM_READ_CACHE_TTL_S", "30") or 30)  # >poll interval so 5s mission/board polls hit the cache (was 3s → every poll missed); PM_READ_CACHE_TTL_S=3 reverts
_READ_CACHE_MAX_ENTRIES = max(1, int(os.environ.get("PM_READ_CACHE_MAX_ENTRIES", "512") or 512))
# Serve-stale-while-revalidate: when an entry's TTL has lapsed but its STAMP is unchanged,
# the cached payload is still correct DATA (the stamp folds in every write) — only the few
# un-stamped signals (claim expiry, presence) may have drifted. So we return it instantly
# and rebuild in the background instead of blocking the polling request on the builder.
# That keeps the once-per-TTL rebuild off the request path (web p99 stays at hit latency)
# while un-stamped staleness is still bounded to ~TTL + one refresh. A CHANGED stamp (real
# data change) still builds synchronously — we never serve data we know is wrong. Kill
# switch: PM_READ_CACHE_STALE_REVALIDATE=0 falls back to synchronous rebuild on expiry.
_READ_CACHE_STALE_REVALIDATE = (os.environ.get("PM_READ_CACHE_STALE_REVALIDATE", "1") or "1") != "0"
_READ_CACHE_REFRESHING: set = set()          # keys with an in-flight background rebuild (single-flight)
_READ_CACHE_LOCK = threading.Lock()          # guards _READ_CACHE_REFRESHING + lazy pool init
_READ_CACHE_REFRESH_POOL = None              # lazily-created ThreadPoolExecutor for background rebuilds


def _store_entry(key: str, stamp: Any, payload: Any, at: Optional[float] = None) -> None:
    """Insert one entry and evict oldest entries at the configured hard bound."""
    _READ_CACHE[key] = {"stamp": stamp, "at": time.time() if at is None else at,
                        "payload": payload}
    overflow = len(_READ_CACHE) - _READ_CACHE_MAX_ENTRIES
    if overflow > 0:
        victims = [item for item in sorted(
            _READ_CACHE, key=lambda item: _READ_CACHE[item]["at"]
        ) if item != key][:overflow]
        for victim in victims:
            if victim != key:
                _READ_CACHE.pop(victim, None)


def _schedule_read_cache_refresh(key: str, stamp: Any, builder: Callable[[], Any]) -> None:
    """Single-flight background rebuild of _READ_CACHE[key] for `stamp`.

    At most one refresh per key runs at a time; extra stale hits during a rebuild just
    re-serve the cached payload. A builder error keeps the stale entry (retried on the next
    expiry). The rebuild only writes back if nothing newer landed meanwhile (a synchronous
    build for a changed stamp always wins), so it can never clobber fresher data.
    """
    global _READ_CACHE_REFRESH_POOL
    with _READ_CACHE_LOCK:
        if key in _READ_CACHE_REFRESHING:
            return
        if _READ_CACHE_REFRESH_POOL is None:
            _READ_CACHE_REFRESH_POOL = ThreadPoolExecutor(
                max_workers=int(os.environ.get("PM_READ_CACHE_REFRESH_WORKERS", "3") or 3),
                thread_name_prefix="ttlcache-refresh")
        _READ_CACHE_REFRESHING.add(key)
        pool = _READ_CACHE_REFRESH_POOL

    def _refresh() -> None:
        try:
            payload = builder()
            cur = _READ_CACHE.get(key)
            if cur is None or cur["stamp"] == stamp:   # don't clobber a newer (changed-stamp) entry
                _store_entry(key, stamp, payload)
        except Exception:
            pass                                        # keep the stale entry; retry on next expiry
        finally:
            with _READ_CACHE_LOCK:
                _READ_CACHE_REFRESHING.discard(key)

    try:
        pool.submit(_refresh)
    except RuntimeError:                                # pool shutting down (process exit)
        with _READ_CACHE_LOCK:
            _READ_CACHE_REFRESHING.discard(key)


def ttl_read_cache(namespace: str, ident: str, stamp: Any,
                   builder: Callable[[], Any], ttl: float = _READ_CACHE_TTL) -> Any:
    """Serve `builder()` from a short-TTL cache keyed by (namespace, ident, stamp).

    Fresh hit (stamp matches, within TTL) returns instantly. A changed stamp forces a
    synchronous rebuild. An expired-but-unchanged-stamp hit is served stale immediately
    with a background refresh (see _schedule_read_cache_refresh / _READ_CACHE for the
    invalidation/staleness contract) so no polling request pays the rebuild.
    """
    key = f"{namespace}\x00{ident}"
    now = time.time()
    hit = _READ_CACHE.get(key)
    if hit is not None and hit["stamp"] == stamp:
        if (now - hit["at"]) < ttl:
            return hit["payload"]                       # fresh
        if _READ_CACHE_STALE_REVALIDATE:
            _schedule_read_cache_refresh(key, stamp, builder)
            return hit["payload"]                        # stale-while-revalidate
    payload = builder()                                  # cold, changed stamp, or revalidate off
    _store_entry(key, stamp, payload, now)
    return payload
