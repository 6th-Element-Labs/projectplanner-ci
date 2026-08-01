#!/usr/bin/env python3
"""Run a bounded, audit-recorded Mission Bot v4 shadow comparison."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from switchboard.application.mission_bot_v4 import run_shadow_batch  # noqa: E402
from switchboard.storage.repositories.mission_journal import (  # noqa: E402
    default_mission_journal_repository,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare v4 proposals with authoritative v1; execute no lifecycle effect.",
    )
    parser.add_argument("--project", default="switchboard")
    parser.add_argument("--actor", default="operator/mission-bot-v4-shadow")
    parser.add_argument("--task-id", action="append", default=[])
    args = parser.parse_args()

    task_ids = list(args.task_id)
    if not task_ids:
        task_ids = default_mission_journal_repository.active_task_ids(
            project=args.project,
        )
    result = run_shadow_batch(
        task_ids,
        project=args.project,
        actor=args.actor,
    )
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
