#!/usr/bin/env python3
"""SIMPLIFY-31: v1 is the only Mission Bot runtime on master."""
from pathlib import Path

from path_setup import ROOT


def read(path: str) -> str:
    return (Path(ROOT) / path).read_text(encoding="utf-8")


for required in (
    "src/switchboard/application/completion_driver.py",
    "src/switchboard/application/mission_bot/driver.py",
    "src/switchboard/domain/completion/state_machine.py",
    "src/switchboard/domain/mission_bot/reducer.py",
    "tests/test_mission_bot.py",
):
    assert (Path(ROOT) / required).is_file(), required

for quarantined in (
    "docs/MISSION-BOT-V4.md",
    "src/switchboard/application/mission_bot_v4",
    "src/switchboard/domain/mission_bot_v4",
    "src/switchboard/application/commands/mission_journal.py",
    "src/switchboard/storage/repositories/mission_journal.py",
):
    assert not (Path(ROOT) / quarantined).exists(), quarantined

coordinator = read("scoped_completion_coordinator.py")
daemon = read("coordinator_daemon.py")
service = read("deploy/projectplanner-coordinator-autopilot.service")
migrations = read("src/switchboard/storage/migrations/runner.py")

assert "run_completion_tick" in coordinator
assert "run_v4_tick" not in coordinator
assert "mission_engine" not in daemon
assert "PM_COORDINATOR_MISSION_ENGINE" not in service
assert "0123_mission_items" not in migrations
assert "0124_mission_events" not in migrations

print("SIMPLIFY-31 v1-only runtime: PASS")
