"""Compatibility façade for Switchboard persistence (ARCH-MS-45).

Callers may keep ``import store``. Implementation lives under
``src/switchboard/storage/repositories/`` (and leaf ``*_store.py`` shims).
This module is an import-only re-export surface — no business logic or SQL.
"""
from __future__ import annotations

import scripts.switchboard_path  # noqa: F401 — make src/switchboard importable
from switchboard.storage.repositories import shell as _shell

# Re-export public names eagerly so ``from store import create_task`` and star-friendly
# tooling keep working without waiting on module __getattr__.
for _name in getattr(_shell, "__all__", None) or dir(_shell):
    if _name.startswith("__") and _name.endswith("__"):
        continue
    globals()[_name] = getattr(_shell, _name)
del _name


def __getattr__(name: str):
    return getattr(_shell, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_shell)))
