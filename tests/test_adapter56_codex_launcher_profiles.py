#!/usr/bin/env python3
"""ADAPTER-56 focused proof for named Codex profiles and isolated launches."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
from types import SimpleNamespace

from path_setup import ROOT  # noqa: F401

from adapters.codex_app_server import (
    AppServerError,
    CodexAppServer,
    persist_launch_receipt,
    profile_overrides,
)
from adapters import agent_host
from switchboard.application.commands import connect_dispatch, task_execution
from switchboard.connect.execution_assignment import (
    ExecutionAssignmentError,
    build_execution_assignment,
)


def _assignment(**lifecycle):
    return build_execution_assignment(
        task_id="ADAPTER-56",
        assignment={"assignment_id": "assignment-adapter56", "runtime": "codex"},
        lifecycle={
            "role": "implementation",
            "execution_id": "execlease-adapter56",
            "generation": 1,
            "pr_number": 0,
            "pr_url": "",
            **lifecycle,
        },
    )


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return (result.stdout or "").strip()


def _connect_wake(index: int, profile: str, *, role: str = "remediation") -> dict:
    """Build one server-shaped Connect wake for the Agent Host seam."""
    assignment = {
        "assignment_id": f"assignment-adapter56-{index}",
        "principal_ref": f"agent/codex/adapter-56-{index}",
        "work_ref": "task:switchboard:ADAPTER-56",
        "runtime": "codex",
        "provider": "openai",
        "workspace_ref": "repo:canonical",
        "limits": {
            "max_runtime_seconds": 7200,
            "spend_limit_microunits": 0,
            "memory_limit_bytes": 0,
        },
        "queued_at": float(1_700_000_001 + index),
    }
    lifecycle = {
        "schema": "switchboard.execution_lifecycle.v1",
        "role": role,
        "head_sha": "a" * 40 if role in {"review_merge", "remediation"} else "",
        "pr_number": 1405,
        "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/1405",
        "ttl_seconds": 7200,
        "context_profile": profile,
        "execution_id": f"execlease-adapter56-{index}",
        "generation": index + 1,
    }
    contract = build_execution_assignment(
        task_id="ADAPTER-56",
        assignment=assignment,
        lifecycle=lifecycle,
    )
    return {
        "wake_id": f"wake-adapter56-{index}",
        "task_id": "ADAPTER-56",
        "selector": {
            "runtime": "codex",
            "provider": "openai",
            "lane": "ADAPTER",
            "agent_id": assignment["principal_ref"],
            "task_id": "ADAPTER-56",
            "capabilities": [
                "execution_lease_v2", "runner_lease_enforcement",
            ],
        },
        "policy": {
            "mode": "connect",
            "assignment": {
                "schema": "switchboard.connect.assignment.v1",
                **assignment,
            },
            "lifecycle": lifecycle,
            "execution_assignment": contract,
            "repository_binding": {
                "schema": "switchboard.repository_binding.v1",
                "project": "switchboard",
                "repo_role": "canonical",
                "repository": "6th-Element-Labs/projectplanner",
                "default_branch": "master",
            },
            "account_binding": {
                "claim_id": f"claim-adapter56-{index}",
                "work_session_id": f"worksession-adapter56-{index}",
            },
        },
        "project": "switchboard",
    }


def _start_projection(profile: str, *, live: bool) -> dict:
    task = {
        "task_id": "ADAPTER-56",
        "_wsId": "ADAPTER",
        "git_state": {
            "head_sha": "b" * 40,
            "branch": "agent/switchboard/ADAPTER-56/execlease-88d30877f8304ffeabc4-g5",
            "pr_number": 1405,
            "pr_url": "https://github.com/6th-Element-Labs/projectplanner/pull/1405",
        },
    }
    if live:
        return {
            "task": task,
            "active_runner": {
                "runner_session_id": "run-adapter56-live",
                "host_id": "host/steve-mbp",
                "metadata": {
                    "codex_launch_receipt": {"profile": profile},
                },
            },
        }
    return {
        "task": task,
        "active_attempt": {
            "wake_id": "wake-adapter56-pending",
            "status": "pending",
            "context_profile": profile,
        },
    }


def _assert_profile_refusal(callable_, expected: str) -> None:
    try:
        callable_()
    except task_execution.TaskExecutionError as exc:
        assert exc.code == "invalid_input", exc.as_dict()
        assert exc.details.get("start_error") == expected, exc.as_dict()
    else:
        raise AssertionError(f"expected {expected} refusal")


def main() -> None:
    profile_dir = Path(ROOT) / "adapters" / "codex" / "profiles"
    expected = {
        "luna-max-large-codebase.config.toml": {
            "model": "gpt-5.6-luna",
            "model_reasoning_effort": "max",
            "model_context_window": 600000,
            "model_auto_compact_token_limit": 500000,
        },
        "luna-max-long-running.config.toml": {
            "model": "gpt-5.6-luna",
            "model_reasoning_effort": "max",
            "model_context_window": 800000,
            "model_auto_compact_token_limit": 720000,
        },
    }
    for name, values in expected.items():
        with (profile_dir / name).open("rb") as stream:
            assert tomllib.load(stream) == values, name

    assert profile_overrides("luna-max-long-running") == [
        'model="gpt-5.6-luna"',
        'model_reasoning_effort="max"',
        "model_context_window=800000",
        "model_auto_compact_token_limit=720000",
    ]
    try:
        profile_overrides("operator-default")
    except AppServerError as exc:
        assert "context_profile_unknown" in str(exc)
    else:  # pragma: no cover - assertion is the test
        raise AssertionError("unknown Codex profile was accepted")
    try:
        profile_overrides(
            "luna-max-long-running", ['model_reasoning_effort="high"'])
    except AppServerError as exc:
        assert "conflicts" in str(exc)
    else:  # pragma: no cover - assertion is the test
        raise AssertionError("drifted effective profile was accepted")

    selected = _assignment(context_profile="luna-max-long-running")
    assert selected["context_profile"] == "luna-max-long-running"
    assert "context_profile" not in _assignment()
    try:
        _assignment(context_profile="not-a-profile")
    except ExecutionAssignmentError as exc:
        assert exc.code == "execution_assignment_context_profile_unknown"
    else:  # pragma: no cover - assertion is the test
        raise AssertionError("unknown profile entered the execution contract")

    root = Path(tempfile.mkdtemp(prefix="adapter56-profiles-"))
    try:
        journal = root / "runner" / "codex-app-server.json"
        binding = {
            "project": "switchboard",
            "task_id": "ADAPTER-56",
            "claim_id": "taskclaim-adapter56",
            "work_session_id": "worksession-adapter56",
            "runner_session_id": "run-adapter56",
            "host_id": "host/steve-mbp",
        }
        server = CodexAppServer(
            ["codex", "app-server"], cwd=str(root), env={},
            http=lambda *_args: {}, binding=binding,
            journal_path=str(journal), context_profile="luna-max-long-running",
        )
        assert server.launch_receipt["profile"] == "luna-max-long-running"
        assert server.launch_receipt["model_context_window"] == 800000
        assert server.launch_receipt["model_auto_compact_token_limit"] == 720000
        persisted = json.loads(journal.read_text(encoding="utf-8"))
        assert persisted["launch_receipt"] == server.launch_receipt
        reloaded = CodexAppServer(
            ["codex", "app-server"], cwd=str(root), env={},
            http=lambda *_args: {}, binding=binding,
            journal_path=str(journal), context_profile="luna-max-long-running",
        )
        assert reloaded.launch_receipt == server.launch_receipt

        source = root / "source"
        source.mkdir()
        _git("init", "-q", "-b", "master", cwd=source)
        _git("config", "user.email", "adapter56@example.test", cwd=source)
        _git("config", "user.name", "ADAPTER-56", cwd=source)
        (source / "README.md").write_text("adapter56\n", encoding="utf-8")
        _git("add", "README.md", cwd=source)
        _git("commit", "-qm", "fixture", cwd=source)

        def launch(index: int) -> dict:
            workspace = root / f"worktree-{index}"
            branch = f"agent/adapter56-{index}"
            _git("worktree", "add", "-q", "-b", branch, str(workspace), "HEAD", cwd=source)
            receipt = persist_launch_receipt(
                root / f"runner-{index}" / "codex-launch-receipt.json",
                "luna-max-large-codebase",
                {
                    "host_id": "host/steve-mbp",
                    "task_id": f"ADAPTER-56-{index}",
                    "claim_id": f"claim-{index}",
                    "work_session_id": f"worksession-{index}",
                },
            )
            return {
                "workspace": str(workspace),
                "branch": _git("branch", "--show-current", cwd=workspace),
                "receipt": receipt,
            }

        with ThreadPoolExecutor(max_workers=4) as pool:
            launches = list(pool.map(launch, range(4)))
        assert len({item["workspace"] for item in launches}) == 4
        assert len({item["branch"] for item in launches}) == 4
        assert len({item["receipt"]["work_session_id"] for item in launches}) == 4
        assert all(item["receipt"]["profile"] == "luna-max-large-codebase"
                   for item in launches)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    host_root = Path(tempfile.mkdtemp(prefix="adapter56-host-"))

    def exercise_host_launch() -> list[dict]:
        source = host_root / "source"
        source.mkdir()
        _git("init", "-q", "-b", "master", cwd=source)
        _git("config", "user.email", "adapter56-host@example.test", cwd=source)
        _git("config", "user.name", "ADAPTER-56 host", cwd=source)
        (source / "README.md").write_text("adapter56 host\n", encoding="utf-8")
        _git("add", "README.md", cwd=source)
        _git("commit", "-qm", "host fixture", cwd=source)
        _git(
            "remote", "add", "origin",
            "https://github.com/6th-Element-Labs/projectplanner.git",
            cwd=source,
        )
        host_inventory = {
            "host_id": "host/steve-mbp",
            "repo_root": str(source),
            "project_source_repo_roots": {"switchboard": str(source)},
            "policy": {"allow_work": True, "allow_global_claim": False},
            "runtimes": [{
                "runtime": "codex",
                "provider": "openai",
                "lanes": ["ADAPTER"],
                "capabilities": [
                    "execution_lease_v2", "runner_lease_enforcement",
                ],
                "policy": {"allow_work": True, "allow_global_claim": False},
            }],
        }
        env_keys = (
            "PM_AGENT_HOST_STATE_DIR", "PM_AGENT_HOST_WORKSPACE_ROOT",
            "PM_AGENT_HOST_REPO_CACHE_ROOT", "PM_AGENT_HOST_RUNNER_DIR",
        )
        saved_env = {key: os.environ.get(key) for key in env_keys}
        os.environ.update({
            "PM_AGENT_HOST_STATE_DIR": str(host_root / "state"),
            "PM_AGENT_HOST_WORKSPACE_ROOT": str(host_root / "workspaces"),
            "PM_AGENT_HOST_REPO_CACHE_ROOT": str(host_root / "cache"),
            "PM_AGENT_HOST_RUNNER_DIR": str(host_root / "runners"),
        })
        saved_run = agent_host.subprocess.run
        saved_token = agent_host._issue_connect_session_mcp_token
        saved_trust = agent_host._ensure_codex_workspace_trusted
        saved_heartbeat = agent_host._heartbeat_while_materializing

        def fake_run(command, **kwargs):
            if not any(str(part).endswith("supervisor.py") for part in command):
                return saved_run(command, **kwargs)
            runner_id = command[command.index("--runner-session-id") + 1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "runner_session_id": runner_id,
                    "status": "running",
                    "pid": 4000 + int(runner_id.rsplit("-", 1)[-1]),
                    "command": list(command),
                }),
                stderr="",
            )

        agent_host.subprocess.run = fake_run
        agent_host._issue_connect_session_mcp_token = (
            lambda *_args, **_kwargs: "dst-adapter56-test")
        agent_host._ensure_codex_workspace_trusted = lambda *_args: None
        agent_host._heartbeat_while_materializing = (
            lambda stop, *_args: stop.wait(0.001))
        try:
            def launch_through_host(index: int) -> dict:
                wake = _connect_wake(
                    index, "luna-max-long-running", role="implementation")
                runner_id = f"run-adapter56-{index}"
                receipt = agent_host.launch(
                    wake, host_inventory, runner_session_id=runner_id)
                return {
                    "argv": receipt["command"],
                    "mode": receipt["wake_mode"],
                    "workspace": receipt["cwd"],
                    "runner_id": runner_id,
                    "receipt": receipt["metadata"]["codex_launch_receipt"],
                }

            with ThreadPoolExecutor(max_workers=4) as pool:
                return list(pool.map(launch_through_host, range(4)))
        finally:
            agent_host.subprocess.run = saved_run
            agent_host._issue_connect_session_mcp_token = saved_token
            agent_host._ensure_codex_workspace_trusted = saved_trust
            agent_host._heartbeat_while_materializing = saved_heartbeat
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    host_launches = exercise_host_launch()
    assert all(item["mode"] == "connect" for item in host_launches)
    assert len({item["workspace"] for item in host_launches}) == 4
    assert len({item["runner_id"] for item in host_launches}) == 4
    for result in host_launches:
        argv = result["argv"]
        assert argv[argv.index("--cwd") + 1] == result["workspace"]
        assert argv[argv.index("--runner-session-id") + 1] == result["runner_id"]
        config = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "-c"
        ]
        assert 'model="gpt-5.6-luna"' in config
        assert 'model_reasoning_effort="max"' in config
        assert "model_context_window=800000" in config
        assert "model_auto_compact_token_limit=720000" in config
        assert result["receipt"]["profile"] == "luna-max-long-running"
        assert result["receipt"]["work_session_id"].endswith(
            result["runner_id"].rsplit("-", 1)[-1])

    saved_projection = task_execution._projection
    saved_live_executions = task_execution.runner_repo.task_live_executions
    saved_ownership = task_execution.coordination_repo.task_start_ownership
    saved_ticket = task_execution.runner_pty_command.mint_ticket_for_session
    saved_capacity = connect_dispatch.capacity_readback
    try:
        task_execution.runner_repo.task_live_executions = (
            lambda *_args, **_kwargs: [])
        task_execution.coordination_repo.task_start_ownership = (
            lambda *_args, **_kwargs: None)
        task_execution.runner_pty_command.mint_ticket_for_session = (
            lambda **_kwargs: {})
        connect_dispatch.capacity_readback = (
            lambda *_args, **_kwargs: {})

        task_execution._projection = (
            lambda *_args, **_kwargs: _start_projection(
                "luna-max-large-codebase", live=True))
        attached = task_execution.start_task(
            "ADAPTER-56",
            project="switchboard",
            role="remediation",
            context_profile="LUNA-MAX-LARGE-CODEBASE",
            operator_launch_authorized=True,
        )
        assert attached["action"] == "attach"
        assert attached["context_profile"] == "luna-max-large-codebase"
        _assert_profile_refusal(
            lambda: task_execution.start_task(
                "ADAPTER-56", project="switchboard", role="remediation",
                context_profile="operator-default",
                operator_launch_authorized=True,
            ),
            "execution_assignment_context_profile_unknown",
        )
        _assert_profile_refusal(
            lambda: task_execution.start_task(
                "ADAPTER-56", project="switchboard", role="remediation",
                context_profile="luna-max-long-running",
                operator_launch_authorized=True,
            ),
            "execution_assignment_context_profile_mismatch",
        )

        task_execution._projection = (
            lambda *_args, **_kwargs: _start_projection(
                "luna-max-large-codebase", live=False))
        starting = task_execution.start_task(
            "ADAPTER-56",
            project="switchboard",
            role="remediation",
            profile="luna-max-large-codebase",
            operator_launch_authorized=True,
        )
        assert starting["action"] == "starting"
        assert starting["context_profile"] == "luna-max-large-codebase"
        _assert_profile_refusal(
            lambda: task_execution.start_task(
                "ADAPTER-56", project="switchboard", role="remediation",
                profile="operator-default",
                operator_launch_authorized=True,
            ),
            "execution_assignment_context_profile_unknown",
        )
        _assert_profile_refusal(
            lambda: task_execution.start_task(
                "ADAPTER-56", project="switchboard", role="remediation",
                profile="luna-max-long-running",
                operator_launch_authorized=True,
            ),
            "execution_assignment_context_profile_mismatch",
        )

        task_execution._projection = lambda *_args, **_kwargs: {
            "task": {"task_id": "ADAPTER-56"},
        }
        fresh = task_execution.start_task(
            "ADAPTER-56",
            project="switchboard",
            role="implementation",
            context_profile="luna-max-long-running",
            operator_launch_authorized=True,
            launcher=lambda *_args, **_kwargs: {
                "dispatched": True, "wake_id": "wake-adapter56-fresh",
            },
        )
        assert fresh["action"] == "started"
        assert fresh["context_profile"] == "luna-max-long-running"
    finally:
        task_execution._projection = saved_projection
        task_execution.runner_repo.task_live_executions = saved_live_executions
        task_execution.coordination_repo.task_start_ownership = saved_ownership
        task_execution.runner_pty_command.mint_ticket_for_session = saved_ticket
        connect_dispatch.capacity_readback = saved_capacity

    shutil.rmtree(host_root, ignore_errors=True)

    print("ADAPTER-56 Codex launcher profiles: PASS")


if __name__ == "__main__":
    main()
