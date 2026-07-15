"""Auth independence ports — Protocols only; no monolith imports.

Adapters that wrap root ``auth`` / ``notify`` / ``store`` live outside this
package (see ``switchboard.api.auth_port_adapters``). ARCH-MS-82.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PasswordHasher(Protocol):
    """Password hashing / verification (production: PBKDF2-SHA256)."""

    def hash(self, password: str) -> str:
        """Return a stored password hash for ``password``."""

    def verify(self, password: str, encoded: str) -> bool:
        """Return True when ``password`` matches ``encoded``."""


@runtime_checkable
class AuthNotifier(Protocol):
    """Outbound notifications owned by the auth bounded context."""

    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        """Deliver a single-use password-reset link to ``to_email``."""


@runtime_checkable
class AuthRegistry(Protocol):
    """Repository port for shared project-registry coupling.

    Auth SQL stays in ``auth.store``; registry connection/bootstrap/catalog
    come through this port so the auth package never imports root ``store``.
    """

    def registry_conn(self) -> sqlite3.Connection:
        """Open (or reuse) the shared project_registry connection."""

    def init_project_registry(self) -> None:
        """Ensure base registry tables exist (users, grants, …)."""

    def project_ids(self) -> list[str]:
        """Return known project ids in canonical order."""

    def projects(self) -> list[dict[str, Any]]:
        """Return active project catalog rows ``[{id, label, ...}]``."""
