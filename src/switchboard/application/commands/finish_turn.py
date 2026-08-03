"""Bounded one-call completion command for managed implementation turns.

The command accepts evidence; it never accepts lifecycle choices.  GitHub readiness
and exact PR-head proof are reused from ``complete_claim`` before the storage command
atomically validates the generation/session/test receipt and invokes the existing C3
completion primitive.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import ValidationError

from switchboard.application.commands import complete_claim as complete_claim_command
from switchboard.application.pr_ready import PullRequestReadyGateway
from switchboard.contracts import validation_error_message
from switchboard.contracts.claims import CompleteClaimCommand, FinishTurnCommand
from switchboard.storage.repositories import finish_turns


FinishTurnFn = Callable[..., dict[str, Any]]


def execute(
    command: FinishTurnCommand,
    *,
    actor: str,
    finish: Optional[FinishTurnFn] = None,
    ensure_ready: Optional[PullRequestReadyGateway] = None,
) -> dict[str, Any]:
    """Prove provider identity, then submit one atomic bounded finish."""
    finisher = finish or finish_turns.finish_turn

    def persist(claim_id: str, *, evidence: Any, actor: str, project: str,
                mission_project: str = "", **_: Any) -> dict[str, Any]:
        return finisher(
            claim_id=claim_id,
            task_id=command.task_id,
            execution_id=command.execution_id,
            generation=command.generation,
            work_session_id=command.work_session_id,
            executed_test_run_id=command.executed_test_run_id,
            evidence=evidence,
            actor=actor,
            project=project,
            mission_project=mission_project,
        )

    return complete_claim_command.execute(
        CompleteClaimCommand(
            claim_id=command.claim_id,
            project=command.project,
            evidence=command.completion_evidence(),
            mission_project=command.mission_project,
        ),
        actor=actor,
        complete=persist,
        ensure_ready=ensure_ready,
    )


def execute_mapping_result(
    data: dict[str, Any],
    *,
    actor: str,
    finish: Optional[FinishTurnFn] = None,
    ensure_ready: Optional[PullRequestReadyGateway] = None,
) -> dict[str, Any]:
    """Validate adapter input and return a structured fail-closed result."""
    try:
        command = FinishTurnCommand.from_mapping(data)
    except ValidationError as exc:
        return {
            "accepted": False,
            "error": "invalid_finish_turn",
            "error_code": "invalid_finish_turn",
            "failure_class": "invalid_input",
            "message": validation_error_message(exc),
        }
    return execute(
        command, actor=actor, finish=finish, ensure_ready=ensure_ready)
