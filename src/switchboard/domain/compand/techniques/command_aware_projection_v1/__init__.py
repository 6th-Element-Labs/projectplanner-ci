"""Typed successful-command projection with exact artifact expansion."""

import hashlib

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    compact_json,
    decode_json_object,
    fixture_scenario,
)


def _diagnostic(line: str) -> bool:
    lowered = line.lower()
    return any(word in lowered for word in ("warning", "error", "failed", "traceback"))


class CommandAwareProjectionTechnique(IsolatedTechnique):
    technique_id = "command-aware-projection-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="commands_checks_and_logs")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        receipt = scenario.get("typed_command_result_receipt")
        output = scenario.get("output_utf8")
        lines = scenario.get("lines")
        exit_status = scenario.get("exit_status")
        if (
            scenario.get("output_kind") not in {"check", "test", "build"}
            or
            not isinstance(receipt, dict)
            or receipt.get("trusted_adapter") is not True
            or receipt.get("truncated") is not False
            or receipt.get("new_suffix") is not True
            or isinstance(exit_status, bool)
            or not isinstance(exit_status, int)
            or exit_status != 0
            or scenario.get("warning_present") is not False
            or scenario.get("passthrough_required") is True
            or not isinstance(output, str)
            or not isinstance(lines, list)
            or not lines
            or any(not isinstance(line, str) for line in lines)
        ):
            return None
        digest = hashlib.sha256(output.encode()).hexdigest()
        if receipt.get("output_sha256") != digest or scenario.get("output_sha256") != digest:
            return None
        diagnostics = [line for line in lines if _diagnostic(line)]
        if diagnostics:
            return None
        proposed = compact_json(
            {
                "version": "commands-v1",
                "command": scenario.get("command"),
                "exit_status": exit_status,
                "head": lines[:2],
                "diagnostics": diagnostics,
                "tail": lines[-2:],
                "artifact_sha256": digest,
            }
        )
        return Proposal(proposed, {"output_line_count": len(lines), "diagnostic_count": 0})

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="commands_checks_and_logs")
        projection = decode_json_object(candidate.proposed)
        if wrapped is None or projection is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        output = scenario.get("output_utf8")
        lines = scenario.get("lines")
        if not isinstance(output, str) or not isinstance(lines, list):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        digest = hashlib.sha256(output.encode()).hexdigest()
        if (
            projection.get("version") != "commands-v1"
            or projection.get("command") != scenario.get("command")
            or projection.get("exit_status") != scenario.get("exit_status")
            or projection.get("head") != lines[:2]
            or projection.get("diagnostics") != []
            or projection.get("tail") != lines[-2:]
            or projection.get("artifact_sha256") != digest
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "command_artifact_expansion", "source_artifact_retained": True},
        )


__all__ = ["CommandAwareProjectionTechnique"]
