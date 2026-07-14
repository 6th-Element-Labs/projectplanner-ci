"""Make repo-root and ``src/`` modules importable from script-style tests.

Switchboard's CI executes each test directly (``python tests/test_*.py``), so
Python starts with ``tests/`` rather than the repository root on ``sys.path``.
New tests import this module before importing application code.
"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

for path in (ROOT, SRC):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)


def entrypoint_source(name: str) -> str:
    """Return thin entrypoint source plus optional ``*_impl.py`` body.

    ARCH-MS-45 moved FastAPI/MCP composition into ``app_impl.py`` /
    ``mcp_server_impl.py`` while keeping ``app.py`` / ``mcp_server.py`` as thin
    re-export surfaces. Source-grep tests should use this helper when they mean
    "the running entrypoint composition", not the line-count façade alone.
    """
    thin_path = ROOT / f"{name}.py"
    text = thin_path.read_text(encoding="utf-8")
    impl_path = ROOT / f"{name}_impl.py"
    if impl_path.is_file():
        text = text + "\n" + impl_path.read_text(encoding="utf-8")
    return text
