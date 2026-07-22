"""Content-blind host launch translation for Connect assignments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contract import Ack, ResourceLimits


class LaunchRefused(RuntimeError):
    """Typed refusal when an Ack does not match host-local configuration."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class HostRuntimeConfig:
    """Provider syntax installed on a host outside the Connect assignment."""

    runtime: str
    provider: str
    executable: str
    arguments_before_note: tuple[str, ...]
    arguments_after_note: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all((self.runtime, self.provider, self.executable)):
            raise ValueError("host_runtime_config_incomplete")
        if not self.arguments_before_note:
            raise ValueError("host_runtime_arguments_required")


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Process input returned to a host supervisor; no process is started here."""

    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...]
    limits: ResourceLimits

    def env_dict(self) -> dict[str, str]:
        return dict(self.environment)


def _via_switchboard_instruction(work_ref: str) -> str:
    """Format the same boot sentence Direct/local workers already use.

    ``work_ref`` stays opaque for Connect routing.  When it follows the
    conventional ``task:{project}:{task_id}`` shape, render the familiar
    ``Do {task} in project {project} via Switchboard.`` line; otherwise keep
    the opaque ref in that sentence.
    """

    parts = str(work_ref or "").split(":")
    if len(parts) == 3 and parts[0] == "task" and parts[1] and parts[2]:
        return f"Do {parts[2]} in project {parts[1]} via Switchboard."
    return f"Do {work_ref} via Switchboard."


def assignment_note(ack: Ack) -> str:
    """The complete, intentionally tiny note given to a newly booted agent."""

    assignment = ack.assignment
    return (
        "Switchboard assigned execution identity: "
        f"agent_id={assignment.principal_ref}; "
        f"assignment_id={assignment.assignment_id}. "
        "Use this exact agent_id for prepare_agent_session, register_agent, "
        "claims, and Work Sessions. Do not derive, slugify, or replace it.\n"
        f"{_via_switchboard_instruction(assignment.work_ref)}"
    )


def build_launch_spec(
    ack: Ack,
    config: HostRuntimeConfig,
    *,
    workspace_path: str,
) -> LaunchSpec:
    """Translate one Ack using provider syntax already configured on the host."""

    assignment = ack.assignment
    if not ack.active:
        raise LaunchRefused("lease_not_active")
    if assignment.runtime != config.runtime:
        raise LaunchRefused("runtime_mismatch")
    if assignment.provider != config.provider:
        raise LaunchRefused("provider_mismatch")
    workspace = Path(workspace_path).expanduser()
    if not workspace.is_absolute():
        raise LaunchRefused("workspace_path_not_absolute")

    note = assignment_note(ack)
    environment = tuple(sorted({
        "SWITCHBOARD_CONNECT_ASSIGNMENT_ID": assignment.assignment_id,
        "SWITCHBOARD_CONNECT_LEASE_ID": ack.lease_id,
        "SWITCHBOARD_CONNECT_PRINCIPAL_REF": assignment.principal_ref,
        "SWITCHBOARD_CONNECT_RUNNER_ID": ack.runner_id,
        "SWITCHBOARD_CONNECT_WORK_REF": assignment.work_ref,
        "SWITCHBOARD_CONNECT_WORKSPACE_REF": assignment.workspace_ref,
    }.items()))
    return LaunchSpec(
        argv=(
            config.executable,
            *config.arguments_before_note,
            note,
            *config.arguments_after_note,
        ),
        cwd=str(workspace),
        environment=environment,
        limits=assignment.limits,
    )
