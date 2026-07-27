#!/usr/bin/env python3
"""CI entrypoint for the gold decision-quality suite.

``scripts/switchboard_ci.sh`` discovers ``test_*.py`` and runs each file as a
script. ``gold_decisions.py`` keeps its diagnostic name; this wrapper makes
the same suite merge-gated.
"""
from __future__ import annotations

import gold_decisions


if __name__ == "__main__":
    raise SystemExit(gold_decisions.main())
