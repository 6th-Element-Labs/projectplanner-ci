"""AuthService — global login / self-service signup / access resolution.

Thin, HTTP-free business logic. Storage is AuthStore (registry DB); password
hashing and notifications arrive via injected ports (ARCH-MS-82); sessions are
JWT cookies.
"""
from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional, Tuple

from . import contracts
from . import deps as auth_deps
from . import rate_limit
from . import session
from . import store as auth_store

_RESET_TTL_SECONDS = 3600  # a reset link is valid for one hour, single use


class AuthError(Exception):
    """Auth failure carrying an HTTP status + machine code.

    retry_after (seconds) is set on throttled (429) responses so the API layer
    can surface a standard Retry-After header.
    """
    def __init__(self, code: str, message: str, status: int = 400,
                 *, retry_after: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after


def _accessible_projects(account: Dict[str, Any]) -> List[Dict[str, Any]]:
    ids = set(auth_store.accessible_project_ids(account["id"], account.get("is_superadmin")))
    return [p for p in auth_deps.registry().projects() if p.get("id") in ids]


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
        auth_deps.hasher().hash(password), is_superadmin=False)
    auth_store.record_login(account["id"])
    token, exp = session.issue(account, ip=ip, user_agent=user_agent)
    # projects is [] for a brand-new user — nothing to see until granted.
    return contracts.public_user(account, _accessible_projects(account)), token, exp


def login(email: str, password: str, *, remember_me: bool = False,
          ip: str = "", user_agent: str = "") -> Tuple[Dict[str, Any], str, float]:
    auth_store.init()
    email_norm = (email or "").strip().lower()
    # Throttle BEFORE the account lookup + password check, so a lockout leaks
    # neither account existence nor password-verification timing.
    wait = rate_limit.check("login", ip=ip, email=email_norm)
    if wait is not None:
        rate_limit.record("login", "throttled", ip=ip, email=email_norm,
                          user_agent=user_agent, reason="locked")
        raise AuthError("rate_limited",
                        "Too many sign-in attempts. Please wait and try again.",
                        429, retry_after=wait)
    account = auth_store.get_user_by_email(email_norm)
    if not account or not account.get("password_hash") or account.get("status") != "active":
        rate_limit.record("login", "failure", ip=ip, email=email_norm,
                          user_id=(account or {}).get("id"),
                          user_agent=user_agent, reason="invalid_credentials")
        raise AuthError("invalid_credentials", "Invalid email or password.", 401)
    if account.get("disabled_at"):
        rate_limit.record("login", "failure", ip=ip, email=email_norm,
                          user_id=account.get("id"), user_agent=user_agent,
                          reason="account_disabled")
        raise AuthError("account_disabled", "This account is disabled.", 403)
    if not auth_deps.hasher().verify(password or "", account["password_hash"]):
        rate_limit.record("login", "failure", ip=ip, email=email_norm,
                          user_id=account.get("id"), user_agent=user_agent,
                          reason="invalid_credentials")
        raise AuthError("invalid_credentials", "Invalid email or password.", 401)
    auth_store.record_login(account["id"])
    rate_limit.record("login", "success", ip=ip, email=email_norm,
                      user_id=account["id"], user_agent=user_agent)
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


def change_password(token: str, current_password: str, new_password: str) -> Dict[str, Any]:
    """Self-service password change for the signed-in user.

    Requires the current password (so a stolen session can't silently reset it),
    enforces a minimum length, refuses a no-op, then rotates the hash and signs
    out every OTHER session (the caller stays logged in).
    """
    auth_store.init()
    account = session.verify(token or "")  # raw account incl. password_hash, or None
    if not account:
        raise AuthError("not_authenticated", "You are not signed in.", 401)
    hasher = auth_deps.hasher()
    if not account.get("password_hash") or not hasher.verify(current_password or "", account["password_hash"]):
        raise AuthError("invalid_current_password", "Your current password is incorrect.", 403)
    if len(new_password or "") < 8:
        raise AuthError("weak_password", "New password must be at least 8 characters.", 422)
    if hasher.verify(new_password, account["password_hash"]):
        raise AuthError("password_unchanged", "New password must be different from the current one.", 422)
    auth_store.set_password(account["id"], hasher.hash(new_password))
    auth_store.revoke_user_sessions(account["id"], keep_token=session.sid_of(token) or "")
    return contracts.public_user(account, _accessible_projects(account))


def _send_reset_email(to_email: str, reset_url: str) -> None:
    auth_deps.notifier().send_password_reset(to_email, reset_url)


def request_password_reset(email: str, base_url: str,
                           *, ip: str = "", user_agent: str = "") -> None:
    """Email a single-use reset link IF the account exists.

    Always returns without signalling whether the email matched an account, so the
    endpoint can't be used to enumerate registered users. Send failures are swallowed
    for the same reason.

    Throttled per-IP and per-email to stop reset-email flooding / enumeration
    sweeps. A 429 leaks nothing new: the limit is driven purely by attempt counts
    the caller controls, independent of whether the address is registered.
    """
    auth_store.init()
    email_norm = (email or "").strip().lower()
    wait = rate_limit.check("reset_request", ip=ip, email=email_norm)
    if wait is not None:
        rate_limit.record("reset_request", "throttled", ip=ip, email=email_norm,
                          user_agent=user_agent, reason="locked")
        raise AuthError("rate_limited",
                        "Too many reset requests. Please wait and try again.",
                        429, retry_after=wait)
    account = auth_store.get_user_by_email(email_norm)
    # Record the attempt whether or not the address matches an account, so the rate
    # limit can't be sidestepped by cycling through unknown emails.
    rate_limit.record("reset_request", "request", ip=ip, email=email_norm,
                      user_id=(account or {}).get("id"), user_agent=user_agent)
    if not account or account.get("disabled_at"):
        return
    raw = secrets.token_urlsafe(32)
    auth_store.create_reset_token(account["id"], raw, _RESET_TTL_SECONDS)
    reset_url = base_url.rstrip("/") + "/reset-password?token=" + raw
    try:
        _send_reset_email(account["email"], reset_url)
    except Exception:
        pass


def reset_password(token: str, new_password: str,
                   *, ip: str = "", user_agent: str = "") -> None:
    """Spend a reset token and set the new password, signing out every session.

    Per-IP throttled so the single-use token can't be brute-forced at speed.
    """
    auth_store.init()
    wait = rate_limit.check("reset_consume", ip=ip)
    if wait is not None:
        rate_limit.record("reset_consume", "throttled", ip=ip,
                          user_agent=user_agent, reason="locked")
        raise AuthError("rate_limited",
                        "Too many attempts. Please wait and try again.",
                        429, retry_after=wait)
    if len(new_password or "") < 8:
        raise AuthError("weak_password", "Password must be at least 8 characters.", 422)
    user_id = auth_store.consume_reset_token(token or "")
    if not user_id:
        rate_limit.record("reset_consume", "failure", ip=ip,
                          user_agent=user_agent, reason="invalid_token")
        raise AuthError("invalid_token", "This reset link is invalid or has expired.", 400)
    auth_store.set_password(user_id, auth_deps.hasher().hash(new_password))
    auth_store.revoke_user_sessions(user_id)  # a reset signs out everywhere
    rate_limit.record("reset_consume", "success", ip=ip, user_id=user_id,
                      user_agent=user_agent)


def can_access_project(token: str, project_id: str) -> bool:
    account = session.verify(token or "")
    if not account:
        return False
    if account.get("is_superadmin"):
        return True
    return project_id in set(auth_store.accessible_project_ids(account["id"], False))
