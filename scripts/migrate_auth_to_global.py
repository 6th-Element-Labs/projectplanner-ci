#!/usr/bin/env python3
"""One-shot: migrate per-project password logins into global auth.

Copies every per-project `principal_passwords` login into the global `users` +
`user_auth` tables so the SAME credentials work at the global email+password login.
pbkdf2 hashes are byte-compatible with the global verifier, so passwords are copied
verbatim — nobody resets. Global login is by EMAIL while per-project logins are
usernames, so a username is mapped to an email (known ones below; else
<login>@taikunai.com). Project owners (project_access.owner_user_id) are marked
superadmin so they see every project, including future ones.

Idempotent: re-running only fills gaps. Requires the Switchboard package on the box.
Run:  cd /opt/projectplanner && set -a && . ./.env && set +a && .venv/bin/python scripts/migrate_auth_to_global.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store  # noqa: E402
import switchboard_path  # noqa: E402,F401
from switchboard.api.routers.auth import store as ast  # noqa: E402

# Per-project logins are usernames; the global login is by email.
EMAIL_MAP = {"steve": "steve@taikunai.com", "root": "root@taikunai.com"}
DEFAULT_DOMAIN = "taikunai.com"


def _owner_pids() -> set:
    with store._registry_conn() as c:
        return {r[0] for r in c.execute(
            "SELECT DISTINCT owner_user_id FROM project_access WHERE owner_user_id IS NOT NULL")}


def _email_for(login: str) -> str:
    return EMAIL_MAP.get((login or "").lower()) or f"{(login or 'user').lower()}@{DEFAULT_DOMAIN}"


def migrate() -> list:
    store.init_project_registry()
    ast.init()
    owners = _owner_pids()
    seen: set = set()
    migrated: list = []
    for proj in store.project_ids():
        try:
            with store._conn(proj) as c:
                rows = c.execute(
                    "SELECT pp.login, pp.password_hash, pp.principal_id, p.display_name "
                    "FROM principal_passwords pp JOIN principals p ON p.id = pp.principal_id "
                    "WHERE p.revoked_at IS NULL"
                ).fetchall()
        except Exception:
            rows = []
        for r in rows:
            login, pwhash, pid, name = r["login"], r["password_hash"], r["principal_id"], r["display_name"]
            if not (pid and pwhash) or pid in seen:
                continue
            seen.add(pid)
            email = _email_for(login)
            is_super = pid in owners
            # 1) ensure a global users row carrying the email
            with store._registry_conn() as c:
                row = c.execute("SELECT id, email FROM users WHERE id=?", (pid,)).fetchone()
                if row:
                    if not row["email"]:
                        c.execute("UPDATE users SET email=? WHERE id=?", (email.lower(), pid))
                else:
                    c.execute(
                        "INSERT INTO users(id, email, display_name, created_at, disabled_at) "
                        "VALUES (?,?,?,?,NULL)", (pid, email.lower(), name or login, time.time()))
            # 2) copy the password + set superadmin (owner sees every project)
            ast.set_password(pid, pwhash)
            ast.set_superadmin(pid, is_super)
            migrated.append({"login": login, "email": email.lower(), "pid": pid, "superadmin": is_super})
    return migrated


def main() -> None:
    migrated = migrate()
    print(f"migrated {len(migrated)} login(s):")
    for m in migrated:
        print(f"  {m['login']:8} -> {m['email']:28} superadmin={m['superadmin']}  ({m['pid']})")
    print("verify (resolve by email + password hash present + active):")
    ok = True
    for m in migrated:
        acct = ast.get_user_by_email(m["email"])
        found = bool(acct)
        has_pw = bool(acct.get("password_hash")) if acct else False
        active = (acct.get("status") == "active") if acct else False
        print(f"  {m['email']:28} found={found} has_pw={has_pw} active={active} superadmin={acct.get('is_superadmin') if acct else None}")
        ok = ok and found and has_pw and active
    print("OK" if ok else "INCOMPLETE — check the rows above")


if __name__ == "__main__":
    main()
