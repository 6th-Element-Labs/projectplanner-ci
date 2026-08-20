from __future__ import annotations

import unittest

from path_setup import ROOT

from switchboard.domain.cli_launch import CliLaunchRequestError, cli_launch_request


class CliLaunchRequestTests(unittest.TestCase):
    def test_task_mode_requires_only_a_prompt(self) -> None:
        request = cli_launch_request({"prompt": "Spell train"})

        self.assertTrue(ROOT.is_dir())
        self.assertEqual(request.mode, "task")
        self.assertFalse(request.has_switchboard_task)
        self.assertEqual(request.boot_prompt(), "Spell train")

    def test_task_mode_accepts_optional_switchboard_context(self) -> None:
        request = cli_launch_request({
            "prompt": "Fill out the document",
            "project": "maxwell",
            "task_id": "doc-17",
        })

        prompt = request.boot_prompt(task_context="Use the supplied source notes.")
        self.assertTrue(request.has_switchboard_task)
        self.assertIn("Project: maxwell", prompt)
        self.assertIn("Task: DOC-17", prompt)
        self.assertIn("Use the supplied source notes.", prompt)

    def test_task_mode_does_not_add_repository_workflow(self) -> None:
        request = cli_launch_request({
            "prompt": "Write chapter 12",
            "profile": "luna-simple",
        })

        prompt = request.boot_prompt()
        self.assertNotIn("Repository:", prompt)
        self.assertNotIn("worktree", prompt)
        self.assertNotIn("Switchboard", prompt)

    def test_coding_mode_accepts_repository_without_switchboard(self) -> None:
        request = cli_launch_request({
            "prompt": "Fix the parser",
            "mode": "coding",
            "repository": "/work/parser",
        })

        self.assertIn("Repository: /work/parser", request.boot_prompt())
        self.assertFalse(request.has_switchboard_task)

    def test_coding_mode_can_resolve_repository_from_switchboard_task(self) -> None:
        request = cli_launch_request({
            "prompt": "Implement the task",
            "mode": "coding",
            "project": "maxwell",
            "task_id": "api-42",
        })

        self.assertIn("resolve from Switchboard task", request.boot_prompt())

    def test_coding_mode_rejects_missing_repository_and_task(self) -> None:
        with self.assertRaisesRegex(
            CliLaunchRequestError,
            "coding_repository_or_switchboard_task_required",
        ):
            cli_launch_request({"prompt": "Fix it", "mode": "coding"})

    def test_telemetry_excludes_prompt_and_working_directory(self) -> None:
        request = cli_launch_request({
            "prompt": "Confidential book chapter",
            "working_directory": "/private/draft",
        })

        telemetry = request.telemetry()
        self.assertNotIn("prompt", telemetry)
        self.assertNotIn("working_directory", telemetry)
        self.assertNotIn("Confidential", repr(telemetry))
        self.assertEqual(len(telemetry["prompt_sha256"]), 64)

    def test_task_id_requires_project(self) -> None:
        with self.assertRaisesRegex(CliLaunchRequestError, "project_required"):
            cli_launch_request({"prompt": "Do it", "task_id": "DOC-17"})


if __name__ == "__main__":
    unittest.main()
