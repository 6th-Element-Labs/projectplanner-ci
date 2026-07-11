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
