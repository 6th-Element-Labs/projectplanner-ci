"""Content-minimizing primitives for the Compand shadow Scan."""

from __future__ import annotations

import hashlib
import re
from typing import Mapping


_RLE_SUFFIX = re.compile(r"^(.*) \[repeated ([1-9][0-9]*) times\]$")
_MAX_COMMAND_BYTES = 1_048_576


class ScanEligibilityError(ValueError):
    """A typed command receipt is not eligible for shadow line RLE."""


class LineRleCandidate:
    """Ephemeral candidate plus safe metrics.

    Only ``candidate_text`` contains model-visible text, is excluded from repr, and must
    be discarded after provider counting. The original text is never retained here.
    """

    __slots__ = (
        "source_artifact_sha256",
        "candidate_artifact_sha256",
        "original_bytes",
        "candidate_bytes",
        "repeated_span_count",
        "repeated_line_count",
        "removed_line_count",
        "_candidate_text",
    )

    def __init__(
        self,
        *,
        source_artifact_sha256: str,
        candidate_artifact_sha256: str,
        original_bytes: int,
        candidate_bytes: int,
        repeated_span_count: int,
        repeated_line_count: int,
        removed_line_count: int,
        candidate_text: str,
    ) -> None:
        self.source_artifact_sha256 = source_artifact_sha256
        self.candidate_artifact_sha256 = candidate_artifact_sha256
        self.original_bytes = original_bytes
        self.candidate_bytes = candidate_bytes
        self.repeated_span_count = repeated_span_count
        self.repeated_line_count = repeated_line_count
        self.removed_line_count = removed_line_count
        self._candidate_text = candidate_text

    @property
    def candidate_text(self) -> str:
        return self._candidate_text

    def __repr__(self) -> str:
        return (
            "LineRleCandidate("
            f"source_artifact_sha256={self.source_artifact_sha256!r}, "
            f"candidate_artifact_sha256={self.candidate_artifact_sha256!r}, "
            f"original_bytes={self.original_bytes}, "
            f"candidate_bytes={self.candidate_bytes}, "
            f"repeated_span_count={self.repeated_span_count}, "
            f"repeated_line_count={self.repeated_line_count}, "
            f"removed_line_count={self.removed_line_count})"
        )


def build_line_rle_candidate(
    receipt: Mapping[str, object],
    *,
    expected_call_id: str,
    output_item: Mapping[str, object],
) -> LineRleCandidate:
    """Validate one new Responses tool-output item and its trusted receipt.

    ``expected_call_id`` comes from the adapter's newly appended suffix boundary.
    Requiring it separately prevents a self-consistent but unrelated receipt/item pair
    from becoming eligibility authority.
    """

    output = _eligible_output(
        receipt,
        expected_call_id=expected_call_id,
        output_item=output_item,
    )
    source_bytes = output.encode("utf-8")
    supplied_hash = str(receipt.get("output_sha256") or "")
    observed_hash = hashlib.sha256(source_bytes).hexdigest()
    if supplied_hash != observed_hash:
        raise ScanEligibilityError("output_sha256 does not bind the supplied output")

    encoded, spans, repeated, removed = encode_line_rle(output)
    candidate_bytes = encoded.encode("utf-8")
    return LineRleCandidate(
        source_artifact_sha256=f"sha256:{observed_hash}",
        candidate_artifact_sha256=(
            f"sha256:{hashlib.sha256(candidate_bytes).hexdigest()}"
        ),
        original_bytes=len(source_bytes),
        candidate_bytes=len(candidate_bytes),
        repeated_span_count=spans,
        repeated_line_count=repeated,
        removed_line_count=removed,
        candidate_text=encoded,
    )


def encode_line_rle(text: str) -> tuple[str, int, int, int]:
    """Collapse consecutive identical complete lines with the line-rle-v1 marker."""

    lines = text.splitlines(keepends=True)
    encoded: list[str] = []
    span_count = repeated_line_count = removed_line_count = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        run_end = index + 1
        while run_end < len(lines) and lines[run_end] == line:
            run_end += 1
        count = run_end - index
        complete = line.endswith(("\n", "\r"))
        if count >= 2 and complete:
            content, ending = _split_line_ending(line)
            encoded.append(f"{content} [repeated {count} times]{ending}")
            span_count += 1
            repeated_line_count += count
            removed_line_count += count - 1
        elif complete and _RLE_SUFFIX.fullmatch(_split_line_ending(line)[0]):
            content, ending = _split_line_ending(line)
            encoded.extend([f"{content} [repeated 1 times]{ending}"] * count)
        else:
            encoded.extend(lines[index:run_end])
        index = run_end
    return "".join(encoded), span_count, repeated_line_count, removed_line_count


def decode_line_rle(text: str) -> str:
    """Exact oracle for strings emitted by :func:`encode_line_rle`."""

    decoded: list[str] = []
    for line in text.splitlines(keepends=True):
        content, ending = _split_line_ending(line)
        match = _RLE_SUFFIX.fullmatch(content)
        if match:
            decoded.extend([match.group(1) + ending] * int(match.group(2)))
        else:
            decoded.append(line)
    return "".join(decoded)


def _eligible_output(
    receipt: Mapping[str, object],
    *,
    expected_call_id: str,
    output_item: Mapping[str, object],
) -> str:
    if not isinstance(expected_call_id, str) or not expected_call_id:
        raise ScanEligibilityError("expected_call_id is required")
    if output_item.get("type") != "function_call_output":
        raise ScanEligibilityError("new suffix item must be function_call_output")
    item_call_id = output_item.get("call_id")
    if item_call_id != expected_call_id:
        raise ScanEligibilityError(
            "function_call_output call_id does not match expected_call_id"
        )
    receipt_call_id = receipt.get("call_id")
    if not isinstance(receipt_call_id, str) or not receipt_call_id:
        raise ScanEligibilityError("receipt call_id is required")
    if receipt_call_id != expected_call_id:
        raise ScanEligibilityError("receipt call_id does not match expected_call_id")

    boolean_primitives = {
        "trusted_adapter": True,
        "truncated": False,
        "signed": False,
        "new_suffix": True,
    }
    for key, expected in boolean_primitives.items():
        if receipt.get(key) is not expected:
            raise ScanEligibilityError(f"{key} is not eligible for line-rle-v1")
    exact = {
        "schema": "compand.command_result.v1",
        "source_kind": "command_result",
        "content_type": "text/plain",
        "encoding": "utf-8",
    }
    for key, expected in exact.items():
        if receipt.get(key) != expected:
            raise ScanEligibilityError(f"{key} is not eligible for line-rle-v1")
    exit_status = receipt.get("exit_status")
    if isinstance(exit_status, bool) or exit_status != 0:
        raise ScanEligibilityError("exit_status must be integer zero")
    output = output_item.get("output")
    if not isinstance(output, str):
        raise ScanEligibilityError("function_call_output.output must be UTF-8 text")
    byte_count = receipt.get("byte_count")
    observed_bytes = len(output.encode("utf-8"))
    if isinstance(byte_count, bool) or byte_count != observed_bytes:
        raise ScanEligibilityError("byte_count does not bind the supplied output")
    if observed_bytes > _MAX_COMMAND_BYTES:
        raise ScanEligibilityError("output exceeds the line-rle-v1 byte limit")
    return output


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""
