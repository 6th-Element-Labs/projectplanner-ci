"""Configurable auth port holders — set by ``auth_port_adapters.configure_auth_ports``.

The auth package never imports root ``store`` / ``auth`` / ``notify``; adapters
outside the package inject implementations here.
"""
from __future__ import annotations

from typing import Optional

from .ports import AuthNotifier, AuthRegistry, PasswordHasher

_hasher: Optional[PasswordHasher] = None
_notifier: Optional[AuthNotifier] = None
_registry: Optional[AuthRegistry] = None


def configure(*, hasher: PasswordHasher, notifier: AuthNotifier,
              registry: AuthRegistry) -> None:
    """Bind production (or test) adapters into the auth package."""
    global _hasher, _notifier, _registry
    _hasher = hasher
    _notifier = notifier
    _registry = registry


def is_configured() -> bool:
    return _hasher is not None and _notifier is not None and _registry is not None


def _ensure() -> None:
    if is_configured():
        return
    # Lazy default: composition root and script-style tests share this path.
    from switchboard.api.auth_port_adapters import configure_auth_ports
    configure_auth_ports()


def hasher() -> PasswordHasher:
    _ensure()
    assert _hasher is not None
    return _hasher


def notifier() -> AuthNotifier:
    _ensure()
    assert _notifier is not None
    return _notifier


def registry() -> AuthRegistry:
    _ensure()
    assert _registry is not None
    return _registry
