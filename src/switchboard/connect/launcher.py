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
        pointer = dict(completion_contract.get("launch_pointer") or {})
        typed_tools = dict(completion_contract.get("typed_tools") or {})
        if typed_tools.get("mission_yield") == "yield_mission":
            stale_handoff = ""
            if (
                str(completion_contract.get("desired_role") or "")
                == "implementation"
                and not int(
                    (completion_contract.get("exact_pr") or {}).get("number") or 0
                )
            ):
                stale_handoff += (
                    "This is an implementation assignment with no persisted PR. "
                    "If no live PR exists for the task, that is the expected build "
                    "state. For a fresh task branch with no upstream, publish the "
                    "untouched branch and record that upstream on the existing Work "
                    "Session before preflight and claim; this bootstrap publication "
                    "is not implementation work. Then claim the task, implement it, "
                    "test it, publish the PR, "
                    "and use complete_claim for the existing ADR-0008 C3 "
                    "surrender/host-ack handoff. Do not yield merely because the "
                    "new task has no PR, and never self-declare Done. "
                )
            stale_handoff += (
                "The mechanical yield below is permitted only when the live "
                "positive PR identity or head differs from the persisted "
                "assignment fence, or that positive identity is missing. When "
                "desired_role is review_merge or remediation and the live PR "
                "number and head exactly match exact_pr and exact_head_sha, do "
                "not yield before doing the assigned role: claim it, inspect the "
                "current evidence, and perform review/merge or remediation. "
                "If a live PR exists and its head differs from exact_head_sha, "
                "perform no "
                "repository write. Resolve the positive PR number and its live "
                "head from persisted Switchboard/GitHub evidence; never use the "
                "fresh workspace or base-branch HEAD as the PR head. Read "
                "get_mission_context, then call "
                "yield_mission for this exact execution_id and generation with "
                "observed_through set to its latest_sequence, outcome=continue, "
                "requested_role=review_merge, and head_sha set to the live head; "
                "do not call report_stale_assignment or agent_requires_human for "
                "that mechanical refresh. If positive PR identity or its exact "
                "head is not persisted, yield outcome=waiting with "
                "requested_role=review_merge and head_sha=exact_head_sha when "
                "the assignment has one (otherwise empty), instead of inventing "
                "either value."
            )
            if str(completion_contract.get("desired_role") or "") == "review_merge":
                stale_handoff += (
                    " When this review records changes_requested with automatic "
                    "findings, the role boundary is yield_mission itself: first "
                    "read the latest mission cursor, then call yield_mission with "
                    "outcome=continue, requested_role=remediation, and the current "
                    "persisted PR head. Do not call abandon_claim or complete_claim "
                    "before that yield; either can terminalize this execution before "
                    "the journal receives the remediation handoff. After a successful "
                    "GitHub merge, call reconcile_task_merge for this exact task and "
                    "verify canonical Done before exiting. Do not call yield_mission "
                    "after merge: outcome=continue would append another review event "
                    "and page a redundant reviewer. Reconciliation only observes "
                    "canonical merge provenance; it does not let the agent declare "
                    "Done."
                )
        else:
            stale_handoff = (
                "If the live PR head differs from exact_head_sha, perform no "
                "repository write and call report_stale_assignment with "
                "expected_head, live_head, PR, and an evidence URL; do not call "
                "agent_requires_human for that mechanical refresh."
            )
        identity = {
            key: completion_contract.get(key)
            for key in (
                "schema",
                "task_id",
                "execution_id",
                "assignment_id",
                "generation",
                "desired_role",
                "exact_head_sha",
                "exact_pr",
                "workspace_assignment",
                "claim_expectations",
                "typed_tools",
                "launch_pointer",
            )
        }
        note += (
            "\nImmutable execution assignment: "
            + json.dumps(identity, sort_keys=True, separators=(",", ":"))
            + " The identity/scope fields (task_id, assignment_id, execution_id, "
              "generation, desired_role, exact_head_sha, exact_pr, "
              "workspace_assignment, and claim_expectations) are server-owned "
              "lifecycle authority. "
              "launch_pointer is a non-authoritative starting pointer, not current "
              "diagnosis or lifecycle truth.\n"
              "Before changing code: (1) read the current Switchboard task and "
              "execution; (2) confirm the live PR and head match the assignment "
              "fence; (3) read all open structured findings for that head; "
              "(4) read PR reviews, inline comments, and conversation comments; "
              "(5) read required checks and failing logs; (6) inspect the current "
              "diff and relevant code; (7) summarize the active requirements in "
              "the terminal, then act. Treat the trigger"
            + (f" ({pointer.get('trigger')})" if pointer.get("trigger") else "")
            + " and evidence URL as pointers only. Fail closed if identity/scope "
              "disagrees with the persisted execution lease. Claim and start "
              "exactly desired_role. "
            + stale_handoff
            + " Do not wait for post-start runner injection."
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
