#!/usr/bin/env python3
"""Run the v4 scoped writer only on an explicitly isolated test server."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coordinator_daemon import DaemonConfig  # noqa: E402
from switchboard.application.mission_bot_v4.coordinator import (  # noqa: E402
    V4ScopedCompletionCoordinator,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="isolated Mission Bot v4 writer")
    parser.add_argument("run", choices=("run",))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if os.environ.get("PM_COORDINATOR_V4_ISOLATED_TEST") != "1":
        raise SystemExit(
            "refusing v4 writer: PM_COORDINATOR_V4_ISOLATED_TEST=1 required"
        )

    import store

    config = DaemonConfig.from_env()
    if not config.act:
        raise SystemExit("refusing v4 writer: PM_COORDINATOR_AUTOPILOT_ACT=1 required")
    daemon = V4ScopedCompletionCoordinator(
        config,
        store_mod=store,
        agent_id=f"{config.actor}/v4-test/{uuid.uuid4().hex[:12]}",
    )
    if args.once:
        print(json.dumps(daemon.tick(), indent=2, sort_keys=True, default=str))
    else:
        daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
