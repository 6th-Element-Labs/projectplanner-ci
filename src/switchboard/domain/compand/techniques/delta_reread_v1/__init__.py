"""Bounded unified-diff reread with exact base-plus-patch verification."""

import difflib
import hashlib

from ..shared import (
    DetectionContext,
    IsolatedTechnique,
    Proposal,
    ReasonCode,
    Recovery,
    TechniqueCandidate,
    fixture_scenario,
)


def _apply_unified(base: str, patch: str) -> str | None:
    source = base.splitlines(keepends=True)
    lines = patch.splitlines(keepends=True)
    output: list[str] = []
    source_index = 0
    index = 0
    while index < len(lines) and not lines[index].startswith("@@"):
        index += 1
    while index < len(lines):
        header = lines[index]
        if not header.startswith("@@"):
            return None
        try:
            old = header.split(" ")[1]
            old_start = int(old.split(",")[0].removeprefix("-"))
        except (IndexError, ValueError):
            return None
        output.extend(source[source_index : old_start - 1])
        source_index = old_start - 1
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line.startswith(" "):
                if source_index >= len(source) or source[source_index] != line[1:]:
                    return None
                output.append(source[source_index])
                source_index += 1
            elif line.startswith("-"):
                if source_index >= len(source) or source[source_index] != line[1:]:
                    return None
                source_index += 1
            elif line.startswith("+"):
                output.append(line[1:])
            elif not line.startswith("\\"):
                return None
            index += 1
    output.extend(source[source_index:])
    return "".join(output)


class DeltaRereadTechnique(IsolatedTechnique):
    technique_id = "delta-reread-v1"

    def propose(self, context: DetectionContext) -> Proposal | None:
        wrapped = fixture_scenario(context.original, family="file_rereads_and_diffs")
        if wrapped is None:
            return None
        scenario, _ = wrapped
        receipt = scenario.get("typed_reread_receipt")
        base = scenario.get("base")
        current = scenario.get("current")
        if (
            scenario.get("binary") is True
            or scenario.get("trusted_file_identity") is not True
            or scenario.get("provider_visible_source_scope") != scenario.get("requester_scope")
            or not isinstance(receipt, dict)
            or receipt.get("trusted_adapter") is not True
            or receipt.get("truncated") is not False
            or not isinstance(base, str)
            or not isinstance(current, str)
            or base == current
        ):
            return None
        patch = "".join(
            difflib.unified_diff(
                base.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile="base",
                tofile="current",
                n=3,
            )
        )
        if not patch or len(patch.encode()) >= int(len(current.encode()) * 0.60):
            return None
        if _apply_unified(base, patch) != current:
            return None
        digest = hashlib.sha256(current.encode()).hexdigest()
        if receipt.get("output_sha256") != digest or scenario.get("current_sha256") != digest:
            return None
        return Proposal(
            patch.encode(),
            {"patch_to_current_ratio": len(patch.encode()) / len(current.encode()), "current_hash_verified": True},
        )

    def recover(self, candidate: TechniqueCandidate) -> Recovery:
        wrapped = fixture_scenario(candidate.original, family="file_rereads_and_diffs")
        if wrapped is None:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        scenario, _ = wrapped
        base = scenario.get("base")
        current = scenario.get("current")
        try:
            patch = candidate.proposed.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION) from exc
        if (
            not isinstance(base, str)
            or not isinstance(current, str)
            or _apply_unified(base, patch) != current
            or hashlib.sha256(current.encode()).hexdigest()
            != scenario.get("current_sha256")
        ):
            raise ValueError(ReasonCode.TECHNIQUE_CONTRACT_VIOLATION)
        return Recovery(
            candidate.original,
            {"recovery_kind": "base_plus_unified_diff", "source_artifact_retained": True},
        )


__all__ = ["DeltaRereadTechnique"]
