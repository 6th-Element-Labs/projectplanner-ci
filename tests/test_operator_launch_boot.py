#!/usr/bin/env python3
"""SESSION-17: opt-in operator/launcher boot via prepare_agent_session(mode=launcher)."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="operator-launch-boot-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ.pop("PM_MCP_TOKEN", None)

from path_setup import ROOT  # noqa: E402
from switchboard.application import session_boot  # noqa: E402
from switchboard.application.commands import connect_dispatch  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(cond: bool, msg: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {msg}")
    else:
        failed += 1
        print(f"FAIL  {msg}")


def test_mode_normalization() -> None:
    for value in ("launch", "OPERATOR", "Start", " launch "):
        ok(session_boot.normalize_session_mode(intent=value) == "launcher",
           f"intent alias -> launcher: {value!r}")
    ok(session_boot.normalize_session_mode(mode="launcher", intent="work") == "launcher",
       "mode wins over conflicting intent")
    ok(session_boot.normalize_session_mode(mode="worker", intent="launch") == "worker",
       "explicit mode=worker wins over launch intent")
    for value in ("", "work", "implement", "unit-test", "cli"):
        ok(session_boot.normalize_session_mode(intent=value) == "worker",
           f"non-launcher stays worker: {value!r}")


def test_launcher_agent_id() -> None:
    aid = session_boot.suggest_agent_id(
        "cursor", "", "COORD-41", "COORD",
        {"title": "attention projection"}, mode="launcher")
    ok(aid == "cursor/launcher", "launcher suggest_agent_id is runtime/launcher")
    explicit = session_boot.suggest_agent_id(
        "cursor", "desktop/steve-launcher", "COORD-41", "COORD",
        {"title": "x"}, mode="launcher")
    ok(explicit == "desktop/steve-launcher", "explicit agent_id wins")
    worker = session_boot.suggest_agent_id(
        "cursor", "", "COORD-41", "COORD",
        {"title": "attention projection"}, mode="")
    ok(worker.startswith("cursor/COORD-41-"), "worker suggest_agent_id task-scoped")


def test_launcher_first_calls_and_prompt() -> None:
    agreement = {"protocol": {"name": "switchboard", "version": "ixp.v1"}}
    calls = session_boot.build_first_calls(
        "switchboard", "cursor/launcher", "cursor", "",
        "COORD-41", "COORD", agreement, mode="launcher",
        launch_runtime="claude")
    tools = [c["tool"] for c in calls]
    ok(tools[:4] == [
        "get_working_agreement", "register_agent",
        "list_unacked_messages", "list_unblock_requests",
    ], "launcher first_calls keep handshake prefix")
    ok("get_project_contract" in tools, "launcher keeps get_project_contract")
    ok("get_task" not in tools, "launcher omits get_task")
    ok("claim_task" not in tools and "claim_next" not in tools,
       "launcher omits claim tools")
    register = next(c for c in calls if c["tool"] == "register_agent")
    ok(register["args"]["task_id"] == "",
       "launcher register_agent uses empty task_id")
    start = next(c for c in calls if c["tool"] == "start_task")
    ok(start["args"] == {
        "task_id": "COORD-41",
        "project": "switchboard",
        "runtime": "claude",
        "role": "implementation",
    }, "launch_runtime is explicit and not launcher runtime")

    prompt = session_boot.build_startup_prompt(
        "switchboard", "cursor/launcher", "COORD-41", "COORD",
        mode="launcher", launch_runtime="codex")
    ok("start_task" in prompt and "do not claim" in prompt.lower(),
       "launcher prompt names start_task and forbids claim")
    ok("worktree" in prompt.lower(), "launcher prompt mentions worktree ban")

    worker_calls = session_boot.build_first_calls(
        "switchboard", "cursor/COORD-41-x", "cursor", "",
        "COORD-41", "COORD", agreement, mode="")
    worker_tools = [c["tool"] for c in worker_calls]
    ok("start_task" not in worker_tools and "get_task" in worker_tools,
       "default first_calls remain worker-shaped")
    worker_register = next(c for c in worker_calls if c["tool"] == "register_agent")
    ok(worker_register["args"]["task_id"] == "COORD-41",
       "worker register_agent still binds task_id")


def test_prepare_opt_in_and_worker_snapshot() -> None:
    store.init_project_registry()
    store.init_db("switchboard")
    created = store.create_task({
        "workstream_id": "BOOT",
        "workstream_name": "Boot lane",
        "title": "Launcher boot fixture",
        "description": "hermetic fixture for mode=launcher",
    }, project="switchboard")
    tid = created["task_id"]

    boot_worker = session_boot.prepare_agent_session(
        runtime="cursor", project="switchboard", task_id=tid, intent="unit-test")
    boot_default = session_boot.prepare_agent_session(
        runtime="cursor", project="switchboard", task_id=tid)
    # Worker payload must not gain launcher contract keys.
    for label, boot in (("intent=unit-test", boot_worker), ("default", boot_default)):
        ok("mode" not in boot, f"{label} omits mode key (byte-stable worker shape)")
        ok("allowed_actions" not in boot, f"{label} omits allowed_actions")
        ok("forbidden_actions" not in boot, f"{label} omits forbidden_actions")
        ok("launch_defaults" not in boot, f"{label} omits launch_defaults")
        tools = [c["tool"] for c in boot["first_calls"]]
        ok("get_task" in tools and "start_task" not in tools,
           f"{label} first_calls stay worker")

    # Snapshot: default vs unknown intent share tool sequence.
    ok([c["tool"] for c in boot_default["first_calls"]]
       == [c["tool"] for c in boot_worker["first_calls"]],
       "worker tool sequence stable across non-launcher intents")

    boot_launch = session_boot.prepare_agent_session(
        runtime="cursor", project="switchboard", task_id=tid,
        mode="launcher", launch_runtime="codex")
    ok(boot_launch.get("mode") == "launcher", "launcher prepare sets mode")
    ok(boot_launch.get("allowed_actions") == ["start_task", "get_task_execution"],
       "launcher allowed_actions contract")
    ok(boot_launch.get("forbidden_actions") == ["claim_task", "claim_next"],
       "launcher forbidden_actions contract")
    ok(boot_launch.get("launch_defaults") == {
        "role": "implementation", "runtime": "codex",
    }, "launch_defaults use launch_runtime not launcher runtime")
    ok(boot_launch.get("agent_id") == "cursor/launcher",
       "prepare(mode=launcher) suggests cursor/launcher")
    launch_tools = [c["tool"] for c in boot_launch["first_calls"]]
    ok("start_task" in launch_tools and "get_task" not in launch_tools,
       "launcher prepare first_calls use start_task")
    register = next(c for c in boot_launch["first_calls"] if c["tool"] == "register_agent")
    ok(register["args"]["task_id"] == "",
       "launcher prepare register has empty task_id")
    # prepare must not create claims as a side effect.
    detail = store.get_task(tid, project="switchboard") or {}
    ok(not (detail.get("active_claims") or []),
       "launcher prepare performs no claim writes")


def test_working_agreement_launcher_sequence() -> None:
    agreement = store.get_working_agreement(project="switchboard")
    ok("session_start_sequence_launcher" in agreement,
       "working agreement advertises launcher sequence")
    seq = agreement["session_start_sequence_launcher"]
    ok(any("start_task" in step for step in seq), "launcher sequence names start_task")
    ok(any("do not claim" in step.lower() for step in seq),
       "launcher sequence forbids claim")
    worker = agreement["session_start_sequence"]
    ok(not any("start_task" in step for step in worker),
       "default sequence remains worker-oriented")


def test_cli_runtime_repair() -> None:
    result = connect_dispatch.enqueue_task(
        {"task_id": "COORD-41", "_wsId": "COORD"},
        project="switchboard", actor="test", runtime="cli")
    ok(result.get("dispatched") is False, "cli runtime does not dispatch")
    ok(result.get("error") == "unsupported_runtime", "typed unsupported_runtime")
    ok(result.get("requested_runtime") == "cli", "requested_runtime preserved")
    ok(result.get("supported_runtimes") == ["codex", "claude"],
       "supported_runtimes from advertised registry")
    ok("do not claim_task" in str(result.get("repair") or "").lower()
       or "do not claim_task" in str(result.get("reason") or "").lower(),
       "repair steers away from claim_task")


if __name__ == "__main__":
    try:
        test_mode_normalization()
        test_launcher_agent_id()
        test_launcher_first_calls_and_prompt()
        test_prepare_opt_in_and_worker_snapshot()
        test_working_agreement_launcher_sequence()
        test_cli_runtime_repair()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
