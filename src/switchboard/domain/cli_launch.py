"""Small, transport-free contract for direct CLI agent launches."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping


class CliLaunchRequestError(ValueError):
    """The requested CLI launch cannot be admitted."""


@dataclass(frozen=True)
class CliLaunchRequest:
    """One CLI assignment before a Capacity adapter starts a process.

    Task mode is intentionally prompt-only. Coding mode adds repository workflow
    context but uses the same runner lifecycle.
    """

    prompt: str
    mode: str = "task"
    profile: str = ""
    working_directory: str = ""
    project: str = ""
    task_id: str = ""
    repository: str = ""

    @property
    def has_switchboard_task(self) -> bool:
        return bool(self.project and self.task_id)

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    def telemetry(self) -> dict[str, Any]:
        """Return safe launch metadata. The prompt is deliberately absent."""
        return {
            "schema": "switchboard.cli_launch.v1",
            "mode": self.mode,
            "profile": self.profile,
            "project": self.project,
            "task_id": self.task_id,
            "repository": self.repository,
            "has_switchboard_task": self.has_switchboard_task,
            "prompt_sha256": self.prompt_sha256,
        }

    def boot_prompt(self, *, task_context: str = "") -> str:
        """Build the assignment without inventing a Switchboard dependency."""
        parts = [self.prompt]
        if self.has_switchboard_task:
            parts.append(
                "\n".join((
                    "Switchboard context:",
                    f"- Project: {self.project}",
                    f"- Task: {self.task_id}",
                    "- Read the current task and acceptance criteria before work.",
                    "- Report material progress and final evidence to this task.",
                ))
            )
            if task_context.strip():
                parts.append(f"Current task context:\n{task_context.strip()}")
        if self.mode == "coding":
            parts.append(
                "\n".join((
                    "Coding mode:",
                    f"- Repository: {self.repository or 'resolve from Switchboard task'}",
                    "- Use the assigned isolated worktree and branch.",
                    "- Test the change and follow the repository PR and CI workflow.",
                ))
            )
        return "\n\n".join(parts)


def cli_launch_request(values: Mapping[str, Any]) -> CliLaunchRequest:
    """Normalize and validate one public direct-CLI launch request."""
    prompt = str(values.get("prompt") or "").strip()
    if not prompt:
        raise CliLaunchRequestError("prompt_required")

    mode = str(values.get("mode") or "task").strip().lower()
    if mode not in {"task", "coding"}:
        raise CliLaunchRequestError("mode_must_be_task_or_coding")

    project = str(values.get("project") or "").strip()
    task_id = str(values.get("task_id") or "").strip().upper()
    repository = str(values.get("repository") or "").strip()
    if task_id and not project:
        raise CliLaunchRequestError("project_required_with_task_id")
    if mode == "coding" and not repository and not (project and task_id):
        raise CliLaunchRequestError("coding_repository_or_switchboard_task_required")

    return CliLaunchRequest(
        prompt=prompt,
        mode=mode,
        profile=str(values.get("profile") or "").strip(),
        working_directory=str(values.get("working_directory") or "").strip(),
        project=project,
        task_id=task_id,
        repository=repository,
    )


__all__ = ["CliLaunchRequest", "CliLaunchRequestError", "cli_launch_request"]
