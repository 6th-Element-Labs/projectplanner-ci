#!/usr/bin/env python3
"""BUG-283: fresh Host workspaces inherit one proven project Python."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from adapters import agent_host
from repository_workspace import WorkspaceMaterializationError


def test_projectplanner_runtime_is_proven_and_precedes_system_python():
    proof = agent_host._project_python_runtime({
        "repository": "6th-Element-Labs/projectplanner",
    })

    assert proof is not None
    assert proof["schema"] == "switchboard.host_python_runtime.v1"
    assert tuple(map(int, proof["python_version"].split("."))) >= (3, 12, 0)
    assert Path(proof["python_executable"]).is_file()
    assert proof["environment"]["PATH"].split(os.pathsep)[0] == str(
        Path(agent_host.sys.executable).parent)
    child = subprocess.run(
        ["python3", "-c", "import sys; print(sys.version_info[:2])"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **proof["environment"]},
    )
    assert str(tuple(agent_host.sys.version_info[:2])) in child.stdout


def test_unsupported_host_python_fails_before_launch_by_name():
    with patch.object(agent_host.sys, "version_info", (3, 9, 6)):
        try:
            agent_host._project_python_runtime({
                "repository": "6th-Element-Labs/projectplanner",
            })
        except WorkspaceMaterializationError as exc:
            assert exc.code == "verification_runtime_unavailable"
            assert exc.details["required_python"] == ">=3.12"
            assert exc.details["observed_python"] == "3.9.6"
        else:
            raise AssertionError("unsupported Host Python was accepted")


def test_other_repositories_are_not_given_projectplanner_runtime_policy():
    with patch.object(agent_host.sys, "version_info", (3, 9, 6)):
        assert agent_host._project_python_runtime({
            "repository": "example/other-repository",
        }) is None


if __name__ == "__main__":
    test_projectplanner_runtime_is_proven_and_precedes_system_python()
    test_unsupported_host_python_fails_before_launch_by_name()
    test_other_repositories_are_not_given_projectplanner_runtime_policy()
    print("BUG-283 Host locked Python proof passed")
