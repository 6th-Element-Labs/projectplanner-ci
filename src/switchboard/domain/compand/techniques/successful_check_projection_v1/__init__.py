"""Long successful-check projection with warning and failure refusal."""

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


class SuccessfulCheckProjectionTechnique(IsolatedTechnique):
    technique_id = "successful-check-projection-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="commands_checks_and_logs")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        receipt = scenario.get("typed_command_result_receipt")
        output = scenario.get("output_utf8")
        lines = scenario.get("lines")
        if (
            scenario.get("output_kind") not in {"check", "test", "build"}
            or scenario.get("exit_status") != 0
            or scenario.get("warning_present") is not False
            or scenario.get("passthrough_required") is True
            or scenario.get("oversized") is True
            or not isinstance(receipt, dict)
            or receipt.get("trusted_adapter") is not True
            or receipt.get("truncated") is not False
            or not isinstance(output, str)
            or not isinstance(lines, list)
            or len(lines) < 80
            or any(not isinstance(line, str) for line in lines)
            or any(
                word in line.lower()
                for line in lines
                for word in ("warning", "error", "failed", "traceback")
            )
        ):
            return None
        digest = hashlib.sha256(output.encode()).hexdigest()
        if receipt.get("output_sha256") != digest or scenario.get("output_sha256") != digest:
            return None
        proposed = compact_json(
            {
                "version": self.technique_id,
                "command": scenario.get("command"),
                "exit_status": 0,
                "head": lines[:2],
                "tail": lines[-2:],
                "artifact_sha256": digest,
            }
        )
        return Proposal(proposed, {"output_line_count": len(lines), "recognized_check": True})

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
            projection.get("version") != self.technique_id
            or projection.get("command") != scenario.get("command")
            or projection.get("exit_status") != 0
            or projection.get("head") != lines[:2]
            or projection.get("tail") != lines[-2:]
            or projection.get("artifact_sha256") != digest
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "check_artifact_expansion", "source_artifact_retained": True},
        )


__all__ = ["SuccessfulCheckProjectionTechnique"]
