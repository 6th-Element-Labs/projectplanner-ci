"""AuthService — global login / self-service signup / access resolution.

Thin, HTTP-free business logic. Storage is AuthStore (registry DB); password
hashing reuses the monolith's pbkdf2 helpers; sessions are JWT cookies.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import auth as pw  # monolith: password_hash / verify_password (pbkdf2_sha256)
import store

from . import contracts
from . import session
from . import store as auth_store


class AuthError(Exception):
    """Auth failure carrying an HTTP status + machine code."""
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _accessible_projects(account: Dict[str, Any]) -> List[Dict[str, Any]]:
    ids = set(auth_store.accessible_project_ids(account["id"], account.get("is_superadmin")))
    return [p for p in store.projects() if p.get("id") in ids]


def register(email: str, display_name: str, password: str,
             *, ip: str = "", user_agent: str = "") -> Tuple[Dict[str, Any], str, float]:
    """Self-service signup. Creates a user with NO project grants (deny-by-default)."""
    auth_store.init()
    email = (email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        raise AuthError("invalid_email", "Enter a valid email address.", 422)
    if len(password or "") < 8:
        raise AuthError("weak_password", "Password must be at least 8 characters.", 422)
    if auth_store.get_user_by_email(email):
        raise AuthError("email_taken", "An account with this email already exists.", 409)
    account = auth_store.create_user(
        email, display_name or email.split("@")[0],
        pw.password_hash(password), is_superadmin=False)
    auth_store.record_login(account["id"])
    token, exp = session.issue(account, ip=ip, user_agent=user_agent)
    # projects is [] for a brand-new user — nothing to see until granted.
    return contracts.public_user(account, _accessible_projects(account)), token, exp


def login(email: str, password: str, *, remember_me: bool = False,
          ip: str = "", user_agent: str = "") -> Tuple[Dict[str, Any], str, float]:
    auth_store.init()
    account = auth_store.get_user_by_email(email)
    if not account or not account.get("password_hash") or account.get("status") != "active":
        raise AuthError("invalid_credentials", "Invalid email or password.", 401)
    if account.get("disabled_at"):
        raise AuthError("account_disabled", "This account is disabled.", 403)
    if not pw.verify_password(password or "", account["password_hash"]):
        raise AuthError("invalid_credentials", "Invalid email or password.", 401)
    auth_store.record_login(account["id"])
    token, exp = session.issue(account, remember_me=remember_me, ip=ip, user_agent=user_agent)
    return contracts.public_user(account, _accessible_projects(account)), token, exp


def current_user(token: str) -> Optional[Dict[str, Any]]:
    """Resolve a session cookie to {user + accessible projects}, or None."""
    account = session.verify(token or "")
    if not account:
        return None
    return contracts.public_user(account, _accessible_projects(account))


def logout(token: str) -> bool:
    return session.revoke(token or "")


def can_access_project(token: str, project_id: str) -> bool:
    account = session.verify(token or "")
    if not account:
        return False
    if account.get("is_superadmin"):
        return True
    return project_id in set(auth_store.accessible_project_ids(account["id"], False))
