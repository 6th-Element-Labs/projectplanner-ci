"""Content-blind host launch translation for Connect assignments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .contract import Ack, ResourceLimits
from .execution_assignment import (
    EXACT_HEAD_ROLES,
    SCHEMA as EXECUTION_ASSIGNMENT_SCHEMA,
)


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


def assignment_note(ack: Ack, completion_contract: dict | None = None) -> str:
    """The complete, intentionally tiny note given to a newly booted agent."""

    assignment = ack.assignment
    note = (
        "Switchboard assigned execution identity: "
        f"agent_id={assignment.principal_ref}; "
        f"assignment_id={assignment.assignment_id}. "
        "Use this exact agent_id for prepare_agent_session, register_agent, "
        "claims, and Work Sessions. Do not derive, slugify, or replace it.\n"
        f"{_via_switchboard_instruction(assignment.work_ref)}"
    )
    if completion_contract:
        note += (
            "\nImmutable execution assignment: "
            + json.dumps(completion_contract, sort_keys=True, separators=(",", ":"))
            + " This server-owned contract is lifecycle authority. Fail closed "
              "before claiming or starting work if task_id, assignment_id, "
              "execution_id, generation, desired_role, or exact_head_sha "
              "disagrees with the persisted execution lease. Claim and start "
              "exactly desired_role, applying its acceptance_findings; do not "
              "infer a different role from board status and do not wait for "
              "post-start runner injection."
        )
    return note


def build_launch_spec(
    ack: Ack,
    config: HostRuntimeConfig,
    *,
    workspace_path: str,
    completion_contract: dict | None = None,
) -> LaunchSpec:
    """Translate one Ack using provider syntax already configured on the host."""

    assignment = ack.assignment
    if not ack.active:
        raise LaunchRefused("lease_not_active")
    if assignment.runtime != config.runtime:
        raise LaunchRefused("runtime_mismatch")
    if assignment.provider != config.provider:
        raise LaunchRefused("provider_mismatch")
    if (completion_contract
            and completion_contract.get("schema") == EXECUTION_ASSIGNMENT_SCHEMA):
        if str(completion_contract.get("assignment_id") or "") != assignment.assignment_id:
            raise LaunchRefused("execution_assignment_id_mismatch")
        parts = str(assignment.work_ref or "").split(":")
        expected_task = (
            parts[2] if len(parts) == 3 and parts[0] == "task" else "")
        if (expected_task and str(completion_contract.get("task_id") or "")
                != expected_task):
            raise LaunchRefused("execution_assignment_task_mismatch")
        role = str(completion_contract.get("desired_role") or "")
        if role not in {"implementation", *EXACT_HEAD_ROLES}:
            raise LaunchRefused("execution_assignment_role_invalid")
        if (role in EXACT_HEAD_ROLES
                and not str(completion_contract.get("exact_head_sha") or "")):
            raise LaunchRefused("execution_assignment_exact_head_missing")
        if not str(completion_contract.get("execution_id") or ""):
            raise LaunchRefused("execution_assignment_execution_id_missing")
        if int(completion_contract.get("generation") or 0) <= 0:
            raise LaunchRefused("execution_assignment_generation_invalid")
    workspace = Path(workspace_path).expanduser()
    if not workspace.is_absolute():
        raise LaunchRefused("workspace_path_not_absolute")

    note = assignment_note(ack, completion_contract)
    environment_values = {
        "SWITCHBOARD_CONNECT_ASSIGNMENT_ID": assignment.assignment_id,
        "SWITCHBOARD_CONNECT_LEASE_ID": ack.lease_id,
        "SWITCHBOARD_CONNECT_PRINCIPAL_REF": assignment.principal_ref,
        "SWITCHBOARD_CONNECT_RUNNER_ID": ack.runner_id,
        "SWITCHBOARD_CONNECT_WORK_REF": assignment.work_ref,
        "SWITCHBOARD_CONNECT_WORKSPACE_REF": assignment.workspace_ref,
    }
    if completion_contract:
        encoded_contract = json.dumps(
            completion_contract, sort_keys=True, separators=(",", ":"))
        environment_values["SWITCHBOARD_EXECUTION_ASSIGNMENT_JSON"] = encoded_contract
        # Compatibility for hosts/adapters introduced by ADAPTER-26.
        environment_values["SWITCHBOARD_COMPLETION_CONTRACT_JSON"] = encoded_contract
    environment = tuple(sorted(environment_values.items()))
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
