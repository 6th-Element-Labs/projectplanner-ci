#!/usr/bin/env python3
"""HOST-4: Coordination names one profile; Capacity proves it before spawn."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from repository_workspace import (
    MaterializedWorkspace,
    WorkspaceMaterializationError,
)
from switchboard.connect import execution_assignment as assignment_contract
from switchboard.connect import verification_profile


PROFILE = assignment_contract.SWITCHBOARD_CI_VERIFICATION_PROFILE
CONTEXT = {
    "schema": "switchboard.execution_context.v1",
    "repository": "6th-Element-Labs/projectplanner",
}
ASSIGNMENT = {
    "assignment_id": "assignment-host4",
    "execution_id": "execlease-host4",
    "generation": 1,
    "workspace_assignment": {"context_digest": "sha256:context"},
}
RUNTIME = {
    "schema": "switchboard.host_python_runtime.v1",
    "python_version": "3.12.9",
    "python_executable": "/verified/.venv/bin/python",
    "python_realpath": "/verified/python3.12",
    "environment": {"PATH": "/verified/.venv/bin:/usr/bin"},
}


def _run(*args, cwd):
    subprocess.run(
        list(args), cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _workspace(*, entrypoint=True):
    root = Path(tempfile.mkdtemp(prefix="host4-profile-"))
    _run("git", "init", "-q", cwd=root)
    _run("git", "config", "user.email", "host4@example.test", cwd=root)
    _run("git", "config", "user.name", "HOST-4 test", cwd=root)
    (root / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "example"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text(
        "example==1.2.3\n", encoding="utf-8",
    )
    (root / "requirements-ci.txt").write_text(
        "# exact CI export\n", encoding="utf-8",
    )
    if entrypoint:
        script = root / "scripts" / "switchboard_ci.sh"
        script.parent.mkdir()
        script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    _run("git", "add", ".", cwd=root)
    _run("git", "commit", "-qm", "profile fixture", cwd=root)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return MaterializedWorkspace(
        path=root,
        branch="agent/switchboard/HOST-4/profile-test",
        head_sha=head,
        cache_path=root.parent / "cache.git",
        receipt_path=root.parent / "receipt.json",
        receipt={"authority_digest": "sha256:authority"},
        workspace_root=root.parent,
    )


def _cause(exc):
    assert exc.code == "verification_runtime_unavailable"
    return exc.details.get("diagnostic_cause")


def test_coordination_selects_a_name_not_a_command():
    assert assignment_contract.verification_profile_for(
        "code_strict", CONTEXT,
    ) == PROFILE
    assert assignment_contract.verification_profile_for(
        "docs_review", CONTEXT,
    ) == ""
    assert assignment_contract.verification_profile_for(
        "code_strict", {"repository": "example/other"},
    ) == ""

    built = assignment_contract.build_execution_assignment(
        task_id="HOST-4",
        assignment={"assignment_id": "assignment-host4"},
        lifecycle={
            "role": "implementation",
            "execution_id": "execlease-host4",
            "generation": 1,
            "session_policy_profile": "code_strict",
            "verification_profile": PROFILE,
        },
        execution_context=CONTEXT,
    )
    assert built["verification_profile"] == PROFILE
    assert "verification_profile" in assignment_contract.CONTRACT_FIELDS
    assert "command" not in built
    assert "scripts/switchboard_ci.sh" not in json.dumps(built)


def test_supported_profile_produces_assignment_bound_digests():
    workspace = _workspace()
    playwright = {
        "browser": "chromium",
        "playwright_version": "1.61.0",
        "executable": "/verified/chromium",
        "launch_verified": True,
    }
    with (
        patch.object(agent_host, "_project_python_runtime", return_value=RUNTIME),
        patch.object(
            verification_profile.importlib.metadata,
            "version",
            return_value="1.2.3",
        ),
        patch.object(
            verification_profile, "_playwright_receipt", return_value=playwright),
    ):
        runtime, receipt = agent_host._prove_verification_profile(
            PROFILE, workspace, CONTEXT, ASSIGNMENT,
        )

    assert runtime == RUNTIME
    assert receipt["schema"] == verification_profile.RECEIPT_SCHEMA
    assert receipt["profile"] == PROFILE
    assert receipt["assignment"] == {
        "assignment_id": "assignment-host4",
        "execution_id": "execlease-host4",
        "generation": 1,
        "context_digest": "sha256:context",
    }
    assert receipt["workspace"]["clean"] is True
    assert receipt["workspace"]["isolated"] is True
    assert receipt["dependencies"]["verified_package_count"] == 1
    for key in ("profile_digest", "receipt_digest"):
        assert receipt[key].startswith("sha256:")
    assert receipt["dependencies"]["lock_digest"].startswith("sha256:")
    assert receipt["dependencies"]["environment_digest"].startswith("sha256:")
    assert receipt["entrypoint"]["digest"].startswith("sha256:")


def test_each_missing_capability_refuses_with_diagnostic_cause():
    clean = _workspace()
    missing_entrypoint = _workspace(entrypoint=False)

    with patch.object(agent_host, "_project_python_runtime", side_effect=(
        agent_host._verification_failure(
            "python_version_unsupported", "Python is too old"))):
        try:
            agent_host._prove_verification_profile(
                PROFILE, clean, CONTEXT, ASSIGNMENT)
        except (WorkspaceMaterializationError,
                verification_profile.VerificationRuntimeError) as exc:
            assert _cause(exc) == "python_version_unsupported"
        else:
            raise AssertionError("missing Python was accepted")

    with patch.object(agent_host, "_project_python_runtime", return_value=RUNTIME):
        try:
            agent_host._prove_verification_profile(
                PROFILE, missing_entrypoint, CONTEXT, ASSIGNMENT)
        except (WorkspaceMaterializationError,
                verification_profile.VerificationRuntimeError) as exc:
            assert _cause(exc) == "canonical_test_entrypoint_unavailable"
        else:
            raise AssertionError("missing canonical entrypoint was accepted")

    with (
        patch.object(agent_host, "_project_python_runtime", return_value=RUNTIME),
        patch.object(
            verification_profile.importlib.metadata,
            "version",
            return_value="9.9.9",
        ),
    ):
        try:
            agent_host._prove_verification_profile(
                PROFILE, clean, CONTEXT, ASSIGNMENT)
        except (WorkspaceMaterializationError,
                verification_profile.VerificationRuntimeError) as exc:
            assert _cause(exc) == "dependency_lock_mismatch"
        else:
            raise AssertionError("lock/environment mismatch was accepted")

    dirty = _workspace()
    (dirty.path / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with patch.object(agent_host, "_project_python_runtime", return_value=RUNTIME):
        try:
            agent_host._prove_verification_profile(
                PROFILE, dirty, CONTEXT, ASSIGNMENT)
        except (WorkspaceMaterializationError,
                verification_profile.VerificationRuntimeError) as exc:
            assert _cause(exc) == "test_environment_dirty"
        else:
            raise AssertionError("dirty isolated environment was accepted")

    with (
        patch.object(agent_host, "_project_python_runtime", return_value=RUNTIME),
        patch.object(
            verification_profile.importlib.metadata,
            "version",
            return_value="1.2.3",
        ),
        patch.object(verification_profile, "_playwright_receipt", side_effect=(
            agent_host._verification_failure(
                "playwright_unavailable", "Chromium is absent"))),
    ):
        try:
            agent_host._prove_verification_profile(
                PROFILE, clean, CONTEXT, ASSIGNMENT)
        except (WorkspaceMaterializationError,
                verification_profile.VerificationRuntimeError) as exc:
            assert _cause(exc) == "playwright_unavailable"
        else:
            raise AssertionError("missing Playwright was accepted")


def test_launch_refuses_before_supervisor_when_profile_proof_fails():
    workspace = _workspace()
    wake = {
        "task_id": "HOST-4",
        "selector": {"runtime": "codex"},
        "policy": {
            "mode": "connect",
            "execution_context": CONTEXT,
            "execution_assignment": {"verification_profile": PROFILE},
        },
    }
    inventory = {"host_id": "host/test", "repo_root": str(ROOT)}
    failure = agent_host._verification_failure(
        "playwright_unavailable", "Chromium is absent")
    with (
        patch.object(agent_host, "connect_workspace_request", return_value={}),
        patch.object(agent_host, "_materialize_for_launch", return_value=workspace),
        patch.object(agent_host, "_prove_verification_profile", side_effect=failure),
        patch.object(agent_host, "launch_command") as launch_command,
    ):
        result = agent_host.launch(
            wake, inventory, runner_session_id="run_host4")

    assert result["started"] is False
    assert result["reason"] == "verification_runtime_unavailable"
    assert result["verification_runtime"]["diagnostic_cause"] == (
        "playwright_unavailable")
    launch_command.assert_not_called()


def test_launch_carries_receipt_into_runner_registration_record():
    workspace = _workspace()
    receipt = {
        "schema": verification_profile.RECEIPT_SCHEMA,
        "profile": PROFILE,
        "receipt_digest": "sha256:receipt",
    }
    wake = {
        "task_id": "HOST-4",
        "selector": {"runtime": "codex"},
        "policy": {
            "mode": "connect",
            "execution_context": CONTEXT,
            "execution_assignment": {
                **ASSIGNMENT,
                "verification_profile": PROFILE,
            },
        },
    }
    inventory = {"host_id": "host/test", "repo_root": str(ROOT)}
    supervisor_receipt = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"runner_session_id": "run_host4", "pid": 123}),
        stderr="",
    )
    with (
        patch.object(agent_host, "connect_workspace_request", return_value={}),
        patch.object(agent_host, "_materialize_for_launch", return_value=workspace),
        patch.object(
            agent_host, "_prove_verification_profile",
            return_value=(RUNTIME, receipt),
        ),
        patch.object(
            agent_host, "launch_command",
            return_value=(["verified-supervisor"], "connect"),
        ),
        patch.object(
            agent_host, "verify_repository_workspace", return_value=workspace,
        ),
        patch.object(agent_host, "_record_workspace_binding"),
        patch.object(agent_host, "_ensure_codex_workspace_trusted"),
        patch.object(
            agent_host, "_issue_connect_session_mcp_token",
            return_value="session-token",
        ),
        patch.object(agent_host.subprocess, "run", return_value=supervisor_receipt),
    ):
        result = agent_host.launch(
            wake, inventory, runner_session_id="run_host4")

    assert result["runner_session_id"] == "run_host4"
    assert result["metadata"]["verification_toolchain_receipt"] == receipt
    assert result["metadata"]["verification_runtime"] == {
        key: value for key, value in RUNTIME.items() if key != "environment"
    }


if __name__ == "__main__":
    test_coordination_selects_a_name_not_a_command()
    test_supported_profile_produces_assignment_bound_digests()
    test_each_missing_capability_refuses_with_diagnostic_cause()
    test_launch_refuses_before_supervisor_when_profile_proof_fails()
    test_launch_carries_receipt_into_runner_registration_record()
    print("HOST-4 locked verification profile: PASS")
