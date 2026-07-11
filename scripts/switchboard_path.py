"""Ensure src/ is on sys.path for `import switchboard` without an editable install."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
_SRC_STR = str(_SRC)
if _SRC_STR not in sys.path:
    sys.path.insert(0, _SRC_STR)
