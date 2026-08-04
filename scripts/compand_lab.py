#!/usr/bin/env python3
"""Replay one fixture through the deterministic Compand Technique Lab wire."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from switchboard.services.compand.lab_cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
