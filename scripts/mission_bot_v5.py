#!/usr/bin/env python3
"""Run the production Mission Bot v5 scoped coordinator."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordinator_daemon import DaemonConfig  # noqa: E402
from switchboard.application.mission_bot_v5.coordinator import (  # noqa: E402
    V5ScopedCompletionCoordinator,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mission Bot v5 scoped coordinator")
    parser.add_argument("run", choices=("run",))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    import store

    config = DaemonConfig.from_env()
    if not config.act:
        raise SystemExit("PM_COORDINATOR_AUTOPILOT_ACT=1 is required")
    daemon = V5ScopedCompletionCoordinator(
        config,
        store_mod=store,
        agent_id=f"{config.actor}/v5/{uuid.uuid4().hex[:12]}",
    )
    if args.once:
        print(json.dumps(daemon.tick(), indent=2, sort_keys=True, default=str))
    else:
        daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
