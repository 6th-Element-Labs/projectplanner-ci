"""Brute-force throttle + login audit for the global auth service (HARDEN-45).

Cheap and in-process: it reads/writes the auth service's own SQLite audit table
(``auth_login_events``) — no new service and no extra dependency, which keeps it
affordable on the 1 GB box. Each guard is evaluated against two independent
scopes, per-IP and per-account, so a flood from one address or against one email
is stopped without penalising everyone else.

The lockout is a sliding window of recent attempts with a small linear backoff:
the more failures pile up beyond the threshold, the longer the window each new
failure extends the lock. A *successful* login clears the account's (and that
IP's) failure streak, so a legitimate user who finally types the right password
is never held back by earlier typos — or by an attacker hammering the same
address. Attempts that are themselves blocked are audited but never count toward
the threshold, so a lockout can't perpetuate itself forever.
"""
from __future__ import annotations

import math
import os
import time
from typing import List, Optional

from . import store as auth_store

# guard -> (max_env, max_default, window_env, window_default, mode, scopes)
#   mode selects which events in the window count toward the threshold:
#     "failures_since_success" — failures after the most recent success (login)
#     "all"                    — every genuine attempt (reset-request rate limit)
#     "failures"               — failed attempts only (reset-token guessing)
_GUARDS = {
    "login": ("PM_AUTH_LOGIN_MAX_FAILURES", 5,
              "PM_AUTH_LOGIN_WINDOW_SECONDS", 900,
              "failures_since_success", ("ip", "account")),
    "reset_request": ("PM_AUTH_RESET_MAX_REQUESTS", 5,
                      "PM_AUTH_RESET_WINDOW_SECONDS", 3600,
                      "all", ("ip", "account")),
    "reset_consume": ("PM_AUTH_RESET_CONSUME_MAX_FAILURES", 10,
                      "PM_AUTH_RESET_CONSUME_WINDOW_SECONDS", 3600,
                      "failures", ("ip",)),
}

_MAX_BACKOFF_ENV = "PM_AUTH_MAX_BACKOFF"
_MAX_BACKOFF_DEFAULT = 4  # a locked scope waits at most window * this

_RETENTION_ENV = "PM_AUTH_EVENT_RETENTION_SECONDS"
_RETENTION_DEFAULT = 30 * 24 * 3600  # keep ~30 days of audit rows, then trim

_COUNTED_OUTCOMES = ("success", "failure", "request")


def _int_env(name: str, default: int) -> int:
    try:
        value = int(float(os.environ.get(name, "")))
        return value if value > 0 else default
    except Exception:
        return default


def _enabled() -> bool:
    return (os.environ.get("PM_AUTH_RATELIMIT", "1").strip().lower()
            not in ("0", "false", "off", "no"))


def _counted_timestamps(events: List[dict], mode: str) -> List[float]:
    """From newest-first {ts, outcome} rows, the timestamps that count here."""
    if mode == "failures_since_success":
        stamps: List[float] = []
        for e in events:  # newest first
            if e["outcome"] == "success":
                break  # a success clears the streak of everything older
            if e["outcome"] == "failure":
                stamps.append(e["ts"])
        return stamps
    if mode == "failures":
        return [e["ts"] for e in events if e["outcome"] == "failure"]
    # "all": every genuine attempt; a self-blocked probe ("throttled") never counts
    return [e["ts"] for e in events if e["outcome"] in _COUNTED_OUTCOMES]


def check(guard: str, *, ip: str = "", email: Optional[str] = None) -> Optional[int]:
    """Seconds to wait if (this IP or this account) is currently locked for
    ``guard``, else None.

    Fails OPEN: any bookkeeping error returns None rather than locking everyone
    out of sign-in.
    """
    cfg = _GUARDS.get(guard)
    if not cfg or not _enabled():
        return None
    max_env, max_def, win_env, win_def, mode, scopes = cfg
    threshold = _int_env(max_env, max_def)
    window = _int_env(win_env, win_def)
    max_backoff = max(1, _int_env(_MAX_BACKOFF_ENV, _MAX_BACKOFF_DEFAULT))
    now = time.time()
    horizon = now - window * max_backoff  # counting reach covers the longest lock
    retry = 0.0
    try:
        for scope in scopes:
            if scope == "ip":
                if not ip:
                    continue
                events = auth_store.recent_auth_events(guard, ip=ip, since_ts=horizon)
            else:  # per-account, keyed on the normalized email
                key = (email or "").strip().lower()
                if not key:
                    continue
                events = auth_store.recent_auth_events(guard, email=key, since_ts=horizon)
            stamps = _counted_timestamps(events, mode)
            if len(stamps) >= threshold:
                over = len(stamps) - threshold
                effective_window = window * min(max_backoff, 1 + over)
                unlock_at = max(stamps) + effective_window
                retry = max(retry, unlock_at - now)
    except Exception:
        return None
    return int(math.ceil(retry)) if retry > 0 else None


def record(guard: str, outcome: str, *, ip: str = "", email: Optional[str] = None,
           user_id: Optional[str] = None, user_agent: str = "", reason: str = "") -> None:
    """Append one attempt to the audit trail. Best-effort — never raises."""
    try:
        auth_store.record_auth_event(
            guard, outcome,
            email=((email or "").strip().lower() or None),
            user_id=user_id, ip=ip, user_agent=user_agent, reason=reason)
        _maybe_prune()
    except Exception:
        pass


_prune_counter = 0


def _maybe_prune() -> None:
    """Trim old audit rows roughly once per 100 writes — deterministic (no RNG),
    so tests stay stable while the table can't grow without bound."""
    global _prune_counter
    _prune_counter += 1
    if _prune_counter % 100 != 0:
        return
    retention = _int_env(_RETENTION_ENV, _RETENTION_DEFAULT)
    auth_store.prune_auth_events(time.time() - retention)
