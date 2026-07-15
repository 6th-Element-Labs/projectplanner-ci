#!/usr/bin/env python3
"""Validate a redacted COORD-17 evidence bundle from a file or stdin."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from switchboard.domain.coordination.coord17_proof import (  # noqa: E402
    build_coord17_acceptance,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit switchboard.coord17_acceptance.v1 from redacted evidence.")
    parser.add_argument(
        "evidence", nargs="?", default="-",
        help="Evidence JSON path, or '-' to read stdin.")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.evidence == "-" else Path(args.evidence).read_text()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "schema": "switchboard.coord17_acceptance.v1",
            "task_id": "COORD-17",
            "passed": False,
            "blockers": [f"invalid_evidence:{type(exc).__name__}"],
        }, sort_keys=True))
        return 2
    result = build_coord17_acceptance(payload)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
