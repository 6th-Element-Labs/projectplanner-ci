"""Monolith adapters for auth ports — live *outside* ``api/routers/auth``.

Wraps root ``auth`` (PBKDF2-SHA256), ``notify`` (SMTP), and ``store`` (registry)
so the auth package stays free of those imports (ARCH-MS-82).
"""
from __future__ import annotations

import sqlite3
from typing import Any

import auth as _auth
import notify as _notify
import store as _store

from switchboard.api.routers.auth import deps as auth_deps
from switchboard.api.routers.auth.ports import AuthNotifier, AuthRegistry, PasswordHasher


class Pbkdf2PasswordHasher:
    """Adapter: root ``auth.password_hash`` / ``verify_password`` (PBKDF2-SHA256)."""

    def hash(self, password: str) -> str:
        return _auth.password_hash(password)

    def verify(self, password: str, encoded: str) -> bool:
        return _auth.verify_password(password, encoded)


class SmtpAuthNotifier:
    """Adapter: password-reset mail via root ``notify.reply`` (dry-runs if SMTP unset)."""

    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        _notify.reply(
            to=to_email,
            subject="Reset your Taikun password",
            text=(
                "We received a request to reset your Taikun password.\n\n"
                f"Create a new password:\n{reset_url}\n\n"
                "This link expires in 1 hour and can be used once. "
                "If you didn't request it, you can safely ignore this email."
            ),
        )


class MonolithAuthRegistry:
    """Adapter: shared project_registry connection + catalog via root ``store``."""

    def registry_conn(self) -> sqlite3.Connection:
        return _store._registry_conn()

    def init_project_registry(self) -> None:
        _store.init_project_registry()

    def project_ids(self) -> list[str]:
        return list(_store.project_ids())

    def projects(self) -> list[dict[str, Any]]:
        return list(_store.projects())


def configure_auth_ports(*, hasher: PasswordHasher | None = None,
                         notifier: AuthNotifier | None = None,
                         registry: AuthRegistry | None = None) -> None:
    """Bind auth package ports (idempotent defaults for tests and app_impl)."""
    auth_deps.configure(
        hasher=hasher or Pbkdf2PasswordHasher(),
        notifier=notifier or SmtpAuthNotifier(),
        registry=registry or MonolithAuthRegistry(),
    )
