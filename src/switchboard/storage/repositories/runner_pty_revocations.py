"""Durable PTY capability-ticket revocation (BUG-75).

Revoked JTIs persist in the project board DB until JWT expiry so multi-instance
relays share the same deny list.
"""
from __future__ import annotations

import time
from typing import Optional

from constants import DEFAULT_PROJECT
from db.connection import _conn

__all__ = [
    "persist_revoked_jti",
    "is_jti_revoked_persisted",
    "purge_expired_revoked_jtis",
    "clear_revoked_jtis_for_tests",
]


def persist_revoked_jti(
    jti: str,
    *,
    expires_at: float,
    project: str = DEFAULT_PROJECT,
    now: float | None = None,
) -> bool:
    token = str(jti or "").strip()
    if not token:
        return False
    ts = float(now if now is not None else time.time())
    exp = max(float(expires_at), ts)
    with _conn(project) as c:
        c.execute(
            "INSERT INTO runner_pty_revoked_jtis(jti, expires_at, revoked_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(jti) DO UPDATE SET "
            "expires_at=CASE WHEN excluded.expires_at > expires_at "
            "THEN excluded.expires_at ELSE expires_at END, "
            "revoked_at=excluded.revoked_at",
            (token, exp, ts),
        )
    return True


def is_jti_revoked_persisted(
    jti: str,
    *,
    project: str = DEFAULT_PROJECT,
    now: float | None = None,
) -> Optional[float]:
    """Return remaining ``expires_at`` when revoked and still live; else None."""
    token = str(jti or "").strip()
    if not token:
        return None
    ts = float(now if now is not None else time.time())
    with _conn(project) as c:
        row = c.execute(
            "SELECT expires_at FROM runner_pty_revoked_jtis WHERE jti=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        expires_at = float(row["expires_at"] if hasattr(row, "keys") else row[0])
        if expires_at <= ts:
            c.execute("DELETE FROM runner_pty_revoked_jtis WHERE jti=?", (token,))
            return None
        return expires_at


def purge_expired_revoked_jtis(
    *,
    project: str = DEFAULT_PROJECT,
    now: float | None = None,
) -> int:
    ts = float(now if now is not None else time.time())
    with _conn(project) as c:
        cur = c.execute(
            "DELETE FROM runner_pty_revoked_jtis WHERE expires_at <= ?",
            (ts,),
        )
        return int(cur.rowcount or 0)


def clear_revoked_jtis_for_tests(project: str = DEFAULT_PROJECT) -> None:
    with _conn(project) as c:
        c.execute("DELETE FROM runner_pty_revoked_jtis")
