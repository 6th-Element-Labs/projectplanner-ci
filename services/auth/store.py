"""AuthStore — the auth service's storage over the shared project_registry.db.

Reuses the existing global tables (`users`, `project_role_grants`, `project_access`,
`org_memberships`) and adds two of its own:
  - user_auth       : password + superadmin + login stats, keyed by users.id
  - auth_sessions_v2 : global (not per-project) session rows for revocation

Deny-by-default: a user with no grants resolves to an empty project list.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

import store  # monolith: reuse the registry path + connection settings


def _conn() -> sqlite3.Connection:
    return store._registry_conn()


def init() -> None:
    """Ensure the base registry + the two auth-owned tables exist."""
    store.init_project_registry()  # creates users / project_role_grants / etc.
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_auth (
                user_id       TEXT PRIMARY KEY,
                password_hash TEXT,
                is_superadmin INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'active',
                last_login    REAL,
                login_count   INTEGER NOT NULL DEFAULT 0,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_sessions_v2 (
                token_hash TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                ip         TEXT,
                user_agent TEXT,
                revoked_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_v2_user ON auth_sessions_v2(user_id);
            CREATE TABLE IF NOT EXISTS password_resets (
                token_hash TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used_at    REAL
            );
            CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);
            """
        )


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _account(c: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    auth = c.execute("SELECT * FROM user_auth WHERE user_id=?", (row["id"],)).fetchone()
    auth = dict(auth) if auth else {}
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "disabled_at": row["disabled_at"],
        "is_superadmin": bool(auth.get("is_superadmin")),
        "status": auth.get("status") or "active",
        "password_hash": auth.get("password_hash"),
        "last_login": auth.get("last_login"),
        "login_count": auth.get("login_count") or 0,
    }


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    if not email:
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
        return _account(c, row) if row else None


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _account(c, row) if row else None


def create_user(email: str, display_name: str, password_hash: str,
                *, is_superadmin: bool = False, user_id: str = "") -> Dict[str, Any]:
    """Create a global user + auth row. Deny-by-default: no project grants are added."""
    email = (email or "").strip().lower()
    now = time.time()
    uid = user_id or ("user-" + uuid.uuid4().hex[:16])
    with _conn() as c:
        c.execute(
            "INSERT INTO users(id, email, display_name, created_at, disabled_at) VALUES (?,?,?,?,NULL)",
            (uid, email, (display_name or email).strip(), now),
        )
        c.execute(
            "INSERT INTO user_auth(user_id, password_hash, is_superadmin, status, login_count, "
            "created_at, updated_at) VALUES (?,?,?,?,0,?,?)",
            (uid, password_hash, 1 if is_superadmin else 0, "active", now, now),
        )
    return get_user(uid)


def set_password(user_id: str, password_hash: str) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO user_auth(user_id, password_hash, created_at, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET password_hash=excluded.password_hash, updated_at=excluded.updated_at",
            (user_id, password_hash, now, now),
        )


def set_superadmin(user_id: str, value: bool) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO user_auth(user_id, is_superadmin, created_at, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_superadmin=excluded.is_superadmin, updated_at=excluded.updated_at",
            (user_id, 1 if value else 0, now, now),
        )


def record_login(user_id: str) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "UPDATE user_auth SET last_login=?, login_count=login_count+1, updated_at=? WHERE user_id=?",
            (now, now, user_id),
        )


# ---- sessions ---------------------------------------------------------------
def create_session(user_id: str, token: str, ttl_seconds: int,
                   *, ip: str = "", user_agent: str = "") -> Dict[str, Any]:
    now = time.time()
    exp = now + max(60, int(ttl_seconds))
    with _conn() as c:
        c.execute(
            "INSERT INTO auth_sessions_v2(token_hash, user_id, created_at, expires_at, ip, user_agent) "
            "VALUES (?,?,?,?,?,?)",
            (token_hash(token), user_id, now, exp, ip, user_agent),
        )
    return {"expires_at": exp}


def user_for_session(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, expires_at, revoked_at FROM auth_sessions_v2 WHERE token_hash=?",
            (token_hash(token),),
        ).fetchone()
    if not row or row["revoked_at"] or float(row["expires_at"]) <= time.time():
        return None
    return get_user(row["user_id"])


def revoke_session(token: str) -> bool:
    if not token:
        return False
    with _conn() as c:
        cur = c.execute(
            "UPDATE auth_sessions_v2 SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            (time.time(), token_hash(token)),
        )
        return cur.rowcount > 0


def revoke_user_sessions(user_id: str, keep_token: str = "") -> int:
    """Revoke every live session for a user, optionally sparing keep_token's own.

    Used on password change so other devices are signed out while the caller
    stays logged in. Returns the number of sessions revoked.
    """
    keep_hash = token_hash(keep_token) if keep_token else ""
    with _conn() as c:
        cur = c.execute(
            "UPDATE auth_sessions_v2 SET revoked_at=? "
            "WHERE user_id=? AND revoked_at IS NULL AND token_hash != ?",
            (time.time(), user_id, keep_hash),
        )
        return cur.rowcount


# ---- password reset tokens (single-use, expiring) ---------------------------
def create_reset_token(user_id: str, raw_token: str, ttl_seconds: int) -> float:
    """Store the HASH of a reset token (never the token itself). Returns expiry."""
    now = time.time()
    exp = now + max(60, int(ttl_seconds))
    with _conn() as c:
        c.execute(
            "INSERT INTO password_resets(token_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (token_hash(raw_token), user_id, now, exp),
        )
    return exp


def consume_reset_token(raw_token: str) -> Optional[str]:
    """Atomically spend a valid, unexpired, unused token; return its user_id or None."""
    if not raw_token:
        return None
    th = token_hash(raw_token)
    now = time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, expires_at, used_at FROM password_resets WHERE token_hash=?",
            (th,),
        ).fetchone()
        if not row or row["used_at"] or float(row["expires_at"]) <= now:
            return None
        cur = c.execute(
            "UPDATE password_resets SET used_at=? WHERE token_hash=? AND used_at IS NULL",
            (now, th),
        )
        return row["user_id"] if cur.rowcount == 1 else None


# ---- access resolution (deny-by-default) ------------------------------------
def accessible_project_ids(user_id: str, is_superadmin: bool) -> List[str]:
    """Which projects this user may see. Superadmin → all; else grants + owned + org."""
    all_ids = store.project_ids()
    if is_superadmin:
        return all_ids
    allow: set = set()
    with _conn() as c:
        # explicit role grants where the subject id is this user (principal or user kind)
        for r in c.execute(
            "SELECT DISTINCT project_id FROM project_role_grants "
            "WHERE subject_id=? AND revoked_at IS NULL", (user_id,)):
            allow.add(r["project_id"])
        # projects this user owns
        for r in c.execute(
            "SELECT project_id FROM project_access WHERE owner_user_id=?", (user_id,)):
            allow.add(r["project_id"])
        # projects belonging to orgs the user is a member of
        for r in c.execute(
            "SELECT pa.project_id FROM project_access pa "
            "JOIN org_memberships om ON om.org_id = pa.org_id WHERE om.user_id=?", (user_id,)):
            allow.add(r["project_id"])
    # preserve store.project_ids() ordering / allowlist
    return [pid for pid in all_ids if pid in allow]
