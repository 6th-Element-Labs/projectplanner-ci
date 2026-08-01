#!/usr/bin/env python3
"""SIMPLIFY-31/COORD-112: v1 is the only Mission Bot runtime on master."""
import re
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

for staged_v4 in (
    "src/switchboard/application/mission_bot_v4",
    "src/switchboard/domain/mission_bot_v4",
):
    assert (Path(ROOT) / staged_v4).is_dir(), staged_v4

for passive_contract in (
    "src/switchboard/application/commands/mission_journal.py",
    "src/switchboard/application/commands/capacity_mission_events.py",
    "src/switchboard/application/commands/human_blocker.py",
    "src/switchboard/storage/repositories/attention.py",
    "src/switchboard/storage/repositories/mission_journal.py",
):
    assert (Path(ROOT) / passive_contract).is_file(), passive_contract

coordinator = read("scoped_completion_coordinator.py")
daemon = read("coordinator_daemon.py")
service = read("deploy/projectplanner-coordinator-autopilot.service")
migrations = read("src/switchboard/storage/migrations/runner.py")
command_exports = read("src/switchboard/application/commands/__init__.py")
passive_commands = read(
    "src/switchboard/application/commands/mission_journal.py"
)
passive_repository = read(
    "src/switchboard/storage/repositories/mission_journal.py"
)
capacity_projection = read(
    "src/switchboard/application/commands/capacity_mission_events.py"
)
human_request_projection = read(
    "src/switchboard/application/commands/human_blocker.py"
)
human_answer_projection = read(
    "src/switchboard/storage/repositories/attention.py"
)
github_projection = read(
    "src/switchboard/application/commands/github_mission_events.py"
)
webhook_projection = read("webhook_inbox.py")

assert "run_completion_tick" in coordinator
assert "run_v4_tick" not in coordinator
assert "run_scoped_mission_tick" not in coordinator
assert "mission_engine" not in daemon
assert "PM_COORDINATOR_MISSION_ENGINE" not in service
assert "0123_mission_items" in migrations
assert "0124_mission_events" in migrations

for live_runtime in (
    coordinator, daemon, service, command_exports,
):
    assert "mission_journal" not in live_runtime

for forbidden_effect in (
    "start_task(",
    "runner_control",
    "human_mission_events",
):
    assert forbidden_effect not in passive_commands
    assert forbidden_effect not in passive_repository
    assert forbidden_effect not in capacity_projection
    assert forbidden_effect not in github_projection

for forbidden_projection_effect in (
    "ensure_item(",
    "create_mission(",
    "update_item(",
    "make_runner_lease_due",
    "complete_claim(",
    "autopilot_scopes",
    "runner_sessions",
    "agent_requires_human",
    "reconcile_task_merge",
    "gh pr merge",
):
    assert forbidden_projection_effect not in github_projection
    assert forbidden_projection_effect not in webhook_projection

assert webhook_projection.count("github_mission_events.project_delivery(") == 1

assert "make_runner_lease_due" not in passive_repository
assert passive_commands.count("make_runner_lease_due") == 1
assert "expected_identity=identity" in passive_commands
assert "run_v4_tick" not in passive_commands
assert "mission_bot_v4" not in human_request_projection
assert "run_scoped_mission_tick" not in human_request_projection
assert "start_task(" not in human_request_projection
assert "run_v4_tick" not in human_answer_projection
assert "run_scoped_mission_tick" not in human_answer_projection
assert "start_task(" not in human_answer_projection

staged_paths = {
    (Path(ROOT) / "src/switchboard/application/commands/mission_journal.py").resolve(),
    (Path(ROOT) / "src/switchboard/application/commands/capacity_mission_events.py").resolve(),
    (Path(ROOT) / "src/switchboard/application/commands/human_blocker.py").resolve(),
    (Path(ROOT) / "src/switchboard/application/commands/github_mission_events.py").resolve(),
    (Path(ROOT) / "src/switchboard/application/queries/mission_context.py").resolve(),
    (Path(ROOT) / "src/switchboard/mcp/tools/mission.py").resolve(),
    (Path(ROOT) / "src/switchboard/storage/repositories/attention.py").resolve(),
    (Path(ROOT) / "src/switchboard/storage/repositories/mission_journal.py").resolve(),
    (Path(ROOT) / "src/switchboard/storage/migrations/runner.py").resolve(),
    (Path(ROOT) / "webhook_inbox.py").resolve(),
}
autopilot_projection = (
    Path(ROOT) / "src/switchboard/application/commands/autopilot.py"
).resolve()
staged_paths.add(autopilot_projection)
staged_paths.update(
    path.resolve()
    for root in (
        Path(ROOT) / "src/switchboard/application/mission_bot_v4",
        Path(ROOT) / "src/switchboard/domain/mission_bot_v4",
    )
    for path in root.rglob("*.py")
)
production_files = list((Path(ROOT) / "src/switchboard").rglob("*.py"))
production_files += list((Path(ROOT) / "adapters").rglob("*.py"))
production_files += list((Path(ROOT) / "db").rglob("*.py"))
production_files += list(Path(ROOT).glob("*.py"))
for path in production_files:
    if path.resolve() in staged_paths:
        continue
    source = path.read_text(encoding="utf-8")
    assert "mission_bot_v4" not in source, path
    assert re.search(r"\bmission_journal\b", source) is None, path
    assert re.search(r"\bmission_items\b", source) is None, path
    assert re.search(r"\bmission_events\b", source) is None, path

autopilot_source = read("src/switchboard/application/commands/autopilot.py")
assert "mission_journal.create_mission(" in autopilot_source
assert autopilot_source.index("mission_journal.create_mission(") < autopilot_source.index(
    "scopes_repo.start_autopilot_scope("
)
for forbidden_autopilot_effect in (
    "mission_bot_v4",
    "run_scoped_mission_tick",
    "tick_scoped_mission",
    "task_execution.start_task",
    "runner_sessions",
):
    assert forbidden_autopilot_effect not in autopilot_source

runtime = read("src/switchboard/application/mission_bot_v4/runtime.py")
worker = read("src/switchboard/application/mission_bot_v4/worker.py")
for forbidden_caller in (
    "coordinator_daemon.py",
    "scoped_completion_coordinator.py",
    "src/switchboard/application/mission_bot/driver.py",
    "src/switchboard/application/commands/__init__.py",
):
    assert "mission_bot_v4" not in read(forbidden_caller), forbidden_caller
assert "start_task" in runtime
assert "task_has_live_execution" in runtime
assert "validate_autopilot_scope_authority" in runtime
assert "while " not in worker

print("SIMPLIFY-31/COORD-113 v1 runtime + opt-in scoped v4 pager: PASS")
