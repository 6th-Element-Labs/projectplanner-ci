#!/usr/bin/env python3
"""Mission Bot v5 is the only production lifecycle writer."""
from pathlib import Path

from path_setup import ROOT


def read(path: str) -> str:
    return (Path(ROOT) / path).read_text(encoding="utf-8")


for removed in (
    "src/switchboard/application/completion_driver.py",
    "src/switchboard/application/completion_shadow.py",
    "src/switchboard/application/mission_bot",
    "src/switchboard/domain/mission_bot",
    "src/switchboard/domain/completion/state_machine.py",
    "src/switchboard/domain/completion/normalize.py",
    "src/switchboard/domain/completion/normalization_law.py",
    "src/switchboard/domain/completion/effects.py",
    "src/switchboard/domain/completion/executor.py",
    "scripts/completion_conformance",
    "scripts/mission_bot_v4_shadow.py",
    "tests/conformance",
):
    assert not (Path(ROOT) / removed).exists(), removed

daemon = read("coordinator_daemon.py")
scoped = read("scoped_completion_coordinator.py")
v5_surface = read("src/switchboard/application/mission_bot_v5/__init__.py")
v5_worker = read("src/switchboard/application/mission_bot_v5/worker.py")

assert "V5ScopedCompletionCoordinator(" in daemon
assert "V4ScopedCompletionCoordinator(" not in daemon
assert "daemon = ScopedCompletionCoordinator(" not in daemon
assert "completion_driver" not in scoped
assert "drain_completion_wakes" not in scoped
assert "complete_completion_wake_for_tick" not in scoped
assert "requires an explicit lifecycle engine" in scoped
assert "run_shadow" not in v5_surface
assert "run_scoped_mission_tick" in v5_surface
assert "mission_bot_v4" not in v5_surface
assert "mission_launch_pointer" not in v5_worker
assert "has_pending_capacity_attempt" not in v5_worker

print("Mission Bot v5-only production writer: PASS")
