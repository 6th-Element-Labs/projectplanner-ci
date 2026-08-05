#!/usr/bin/env python3
"""Build or verify the sanitized QA-56 Compand Phase 2 fixture corpus.

The development and golden partitions are deterministic, reviewable fixtures.  The
hidden partition deliberately contains only an immutable custody plan: payloads and
oracles must be supplied later by the independent QA-58 custodian and are never made
available to technique-plugin authors through this repository.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = ROOT / "fixtures" / "compand" / "phase2-technique-corpus" / "v1"
MANIFEST_PATH = ROOT / "docs" / "compand" / "phase2" / "corpus-manifest.json"
CATALOG_PATH = ROOT / "docs" / "compand" / "phase2" / "technique-catalog.json"
VERSION = "1.0.4-qa56"
SCHEMA = "compand.ces1.fixture.v2"
CASE_SCHEMA = "compand.ces1.case_record.v1"
COMMAND_RECEIPT_SCHEMA = "compand.command_result.v1"
FILE_REREAD_RECEIPT_SCHEMA = "compand.file_reread.v1"
MAX_COMMAND_OUTPUT_BYTES = 1_048_576
CANCELLATION_FIXTURE_PATH = (
    ROOT
    / "fixtures"
    / "compand"
    / "openai-responses"
    / "codex-cli-0.144.5"
    / "responses"
    / "cancellation.sse"
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def byte_overlap_ratio(source: str, candidate: str) -> float:
    """Return the reproducible byte-sequence similarity used by overlap fixtures."""
    return difflib.SequenceMatcher(
        None, source.encode("utf-8"), candidate.encode("utf-8"), autojunk=False
    ).ratio()


def _fastcdc_chunks(
    data: bytes, *, minimum: int = 512, target: int = 2048, maximum: int = 8192
) -> list[dict[str, Any]]:
    """Return deterministic content-defined chunks with frozen FastCDC-style bounds."""
    if not (0 < minimum <= target <= maximum):
        raise ValueError("invalid FastCDC bounds")
    mask = target - 1
    if target & mask:
        raise ValueError("FastCDC target must be a power of two")
    gear = [
        int.from_bytes(hashlib.sha256(bytes([value])).digest()[:8], "big")
        for value in range(256)
    ]
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(data):
        cursor = start
        rolling = 0
        while cursor < len(data):
            rolling = ((rolling << 1) + gear[data[cursor]]) & ((1 << 64) - 1)
            cursor += 1
            size = cursor - start
            if size >= minimum and ((rolling & mask) == 0 or size >= maximum):
                break
        chunk = data[start:cursor]
        chunks.append(
            {
                "index": len(chunks),
                "offset": start,
                "byte_count": len(chunk),
                "sha256": sha256_bytes(chunk),
            }
        )
        start = cursor
    return chunks


def _split_sse_frames(body: str) -> list[str]:
    """Split an SSE body without normalizing or synthesizing any bytes."""
    frames: list[str] = []
    cursor = 0
    while cursor < len(body):
        boundary = body.find("\n\n", cursor)
        if boundary < 0:
            frames.append(body[cursor:])
            break
        frames.append(body[cursor : boundary + 2])
        cursor = boundary + 2
    return frames


@dataclass(frozen=True)
class Family:
    family_id: str
    workload_stratum: str
    techniques: tuple[str, ...]
    positive_cases: tuple[str, ...]
    boundary_cases: tuple[str, ...]
    negative_cases: tuple[str, ...]
    dispositions_by_case: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    invariants: tuple[str, ...]


def _case_rules(
    cases: tuple[str, ...], *technique_dispositions: tuple[str, str]
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Materialize an explicit technique-disposition map for every named case."""
    return tuple((case_id, technique_dispositions) for case_id in cases)


FAMILIES = (
    Family(
        "repeated_lines",
        "command_output",
        ("line-rle-v1",),
        ("positive_run",),
        ("single_line", "count_not_smaller"),
        ("mixed_endings", "warning_output", "off_by_one_repeat_mutant"),
        _case_rules(("positive_run",), ("line-rle-v1", "transform"))
        + _case_rules(
            ("single_line", "count_not_smaller"),
            ("line-rle-v1", "decline_framing_overhead"),
        )
        + _case_rules(
            ("mixed_endings", "warning_output", "off_by_one_repeat_mutant"),
            ("line-rle-v1", "detect_corruption"),
        ),
        ("consecutive_complete_lines_only", "exact_decode_and_sha256"),
    ),
    Family(
        "exact_and_partial_overlap",
        "parallel_tool_output",
        (
            "exact-duplicate-reference-v1",
            "subresult-chunk-dedup-v1",
            "parallel-overlap-dedup-v1",
        ),
        ("exact_visible_source",),
        ("missing_source", "evicted_source", "below_70_percent_overlap"),
        ("cross_session_hash",),
        _case_rules(
            ("exact_visible_source",),
            ("exact-duplicate-reference-v1", "transform"),
            ("subresult-chunk-dedup-v1", "transform"),
            ("parallel-overlap-dedup-v1", "transform"),
        )
        + _case_rules(
            ("missing_source", "evicted_source"),
            ("exact-duplicate-reference-v1", "fail_recovery"),
            ("subresult-chunk-dedup-v1", "fail_recovery"),
            ("parallel-overlap-dedup-v1", "fail_recovery"),
        )
        + _case_rules(
            ("below_70_percent_overlap",),
            ("exact-duplicate-reference-v1", "no_material_opportunity"),
            ("subresult-chunk-dedup-v1", "no_material_opportunity"),
            ("parallel-overlap-dedup-v1", "decline_framing_overhead"),
        )
        + _case_rules(
            ("cross_session_hash",),
            ("exact-duplicate-reference-v1", "reject_cross_scope_access"),
            ("subresult-chunk-dedup-v1", "reject_cross_scope_access"),
            ("parallel-overlap-dedup-v1", "reject_cross_scope_access"),
        ),
        ("provider_visible_source_required", "scoped_exact_reconstruction"),
    ),
    Family(
        "structured_json_and_tables",
        "typed_structured_output",
        ("json-minify-v1", "structured-data-codec-v1"),
        ("ordered_rows", "nulls", "numeric_vs_string"),
        ("duplicate_keys", "heterogeneous_rows"),
        ("invalid_json", "changed_order_mutant"),
        _case_rules(
            ("ordered_rows", "nulls", "numeric_vs_string"),
            ("json-minify-v1", "transform"),
            ("structured-data-codec-v1", "transform"),
        )
        + _case_rules(
            ("duplicate_keys", "heterogeneous_rows"),
            ("json-minify-v1", "decline_ineligible"),
            ("structured-data-codec-v1", "decline_ineligible"),
        )
        + _case_rules(
            ("invalid_json", "changed_order_mutant"),
            ("json-minify-v1", "detect_corruption"),
            ("structured-data-codec-v1", "detect_corruption"),
        ),
        ("preserve_scalar_types", "preserve_key_row_and_column_order"),
    ),
    Family(
        "terminal_and_progress",
        "terminal_output",
        ("ansi-osc-strip-v1", "line-ending-normalize-v1", "trailing-noise-trim-v1"),
        ("sgr", "osc8", "carriage_return_spinner"),
        ("source_code_crlf", "signature_bytes"),
        ("unknown_control", "warning_output"),
        _case_rules(
            ("sgr", "osc8"),
            ("ansi-osc-strip-v1", "transform"),
            ("line-ending-normalize-v1", "no_material_opportunity"),
            ("trailing-noise-trim-v1", "no_material_opportunity"),
        )
        + _case_rules(
            ("carriage_return_spinner",),
            ("ansi-osc-strip-v1", "no_material_opportunity"),
            ("line-ending-normalize-v1", "no_material_opportunity"),
            ("trailing-noise-trim-v1", "transform"),
        )
        + _case_rules(
            ("source_code_crlf", "signature_bytes"),
            ("ansi-osc-strip-v1", "decline_ineligible"),
            ("line-ending-normalize-v1", "decline_ineligible"),
            ("trailing-noise-trim-v1", "decline_ineligible"),
        )
        + _case_rules(
            ("unknown_control", "warning_output"),
            ("ansi-osc-strip-v1", "detect_corruption"),
            ("line-ending-normalize-v1", "detect_corruption"),
            ("trailing-noise-trim-v1", "detect_corruption"),
        ),
        ("visible_text_preserved", "warnings_and_integrity_bytes_untouched"),
    ),
    Family(
        "file_rereads_and_diffs",
        "file_tool_output",
        ("unchanged-file-identity-v1", "delta-reread-v1"),
        ("unchanged", "small_edit"),
        ("high_churn", "binary"),
        ("stale_base", "edited_history_fork"),
        _case_rules(
            ("unchanged",),
            ("unchanged-file-identity-v1", "transform"),
            ("delta-reread-v1", "no_material_opportunity"),
        )
        + _case_rules(
            ("small_edit",),
            ("unchanged-file-identity-v1", "no_material_opportunity"),
            ("delta-reread-v1", "transform"),
        )
        + _case_rules(
            ("high_churn",),
            ("unchanged-file-identity-v1", "no_material_opportunity"),
            ("delta-reread-v1", "decline_framing_overhead"),
        )
        + _case_rules(
            ("binary",),
            ("unchanged-file-identity-v1", "decline_ineligible"),
            ("delta-reread-v1", "decline_ineligible"),
        )
        + _case_rules(
            ("stale_base", "edited_history_fork"),
            ("unchanged-file-identity-v1", "fail_recovery"),
            ("delta-reread-v1", "fail_recovery"),
        ),
        ("trusted_file_identity", "base_plus_patch_matches_current_sha256"),
    ),
    Family(
        "commands_checks_and_logs",
        "check_output",
        ("command-aware-projection-v1", "successful-check-projection-v1"),
        ("successful_long_check", "test_output", "build_output"),
        (
            "failed_check",
            "warning_on_success",
            "ambiguous_exit_status",
            "git_status",
            "git_diff",
        ),
        ("unknown_version", "truncated", "oversized", "log_output"),
        _case_rules(
            ("successful_long_check", "test_output", "build_output"),
            ("command-aware-projection-v1", "transform"),
            ("successful-check-projection-v1", "transform"),
        )
        + _case_rules(
            (
                "failed_check",
                "warning_on_success",
                "ambiguous_exit_status",
                "git_status",
                "git_diff",
            ),
            ("command-aware-projection-v1", "decline_ineligible"),
            ("successful-check-projection-v1", "decline_ineligible"),
        )
        + _case_rules(
            ("unknown_version", "truncated", "oversized", "log_output"),
            ("command-aware-projection-v1", "decline_ineligible"),
            ("successful-check-projection-v1", "decline_ineligible"),
        ),
        ("integer_exit_status_authoritative", "diagnostic_order_and_tail_preserved"),
    ),
    Family(
        "cache_sensitive_prefixes",
        "cache_conditioned_request",
        ("prefix-cache-shaping-v1", "provider-kv-reuse-v1", "transport-gzip-v1"),
        ("cold", "warm_same_prefix"),
        ("warm_changed_suffix", "smaller_but_more_expensive"),
        ("expired", "missing_cache_fields"),
        _case_rules(
            ("cold", "warm_same_prefix"),
            ("prefix-cache-shaping-v1", "transform"),
            ("provider-kv-reuse-v1", "decline_ineligible"),
            ("transport-gzip-v1", "no_material_opportunity"),
        )
        + _case_rules(
            ("warm_changed_suffix",),
            ("prefix-cache-shaping-v1", "decline_cache_harm"),
            ("provider-kv-reuse-v1", "no_material_opportunity"),
            ("transport-gzip-v1", "no_material_opportunity"),
        )
        + _case_rules(
            ("smaller_but_more_expensive",),
            ("prefix-cache-shaping-v1", "decline_cache_harm"),
            ("provider-kv-reuse-v1", "decline_cache_harm"),
            ("transport-gzip-v1", "decline_cache_harm"),
        )
        + _case_rules(
            ("expired",),
            ("prefix-cache-shaping-v1", "no_material_opportunity"),
            ("provider-kv-reuse-v1", "no_material_opportunity"),
            ("transport-gzip-v1", "no_material_opportunity"),
        )
        + _case_rules(
            ("missing_cache_fields",),
            ("prefix-cache-shaping-v1", "detect_corruption"),
            ("provider-kv-reuse-v1", "detect_corruption"),
            ("transport-gzip-v1", "no_material_opportunity"),
        ),
        ("frozen_prefix_byte_identity", "provider_usage_missing_is_not_zero"),
    ),
    Family(
        "cooperative_context",
        "cooperative_agent_context",
        (
            "context-paging-v1",
            "schema-deferral-v1",
            "turn-elimination-v1",
            "agent-memory-summary-v1",
            "code-action-batching-v1",
        ),
        ("certified_context_epoch", "ordered_batch"),
        ("no_cloud_seam", "tool_discovery_needed", "no_op_poll"),
        ("failed_expansion", "changed_order_mutant"),
        _case_rules(
            ("certified_context_epoch", "ordered_batch"),
            ("context-paging-v1", "decline_ineligible"),
            ("schema-deferral-v1", "decline_ineligible"),
            ("turn-elimination-v1", "decline_ineligible"),
            ("agent-memory-summary-v1", "decline_ineligible"),
            ("code-action-batching-v1", "decline_ineligible"),
        )
        + _case_rules(
            ("no_cloud_seam", "tool_discovery_needed", "no_op_poll"),
            ("context-paging-v1", "decline_ineligible"),
            ("schema-deferral-v1", "decline_ineligible"),
            ("turn-elimination-v1", "no_material_opportunity"),
            ("agent-memory-summary-v1", "decline_ineligible"),
            ("code-action-batching-v1", "decline_ineligible"),
        )
        + _case_rules(
            ("failed_expansion", "changed_order_mutant"),
            ("context-paging-v1", "fail_recovery"),
            ("schema-deferral-v1", "detect_corruption"),
            ("turn-elimination-v1", "detect_corruption"),
            ("agent-memory-summary-v1", "fail_recovery"),
            ("code-action-batching-v1", "detect_corruption"),
        ),
        ("cooperative_seam_required", "tool_event_order_preserved"),
    ),
    Family(
        "behavioral_and_lossy",
        "behavioral_candidate",
        (
            "semantic-cache-v1",
            "injected-efficiency-instructions-v1",
            "injected-text-hard-compression-v1",
            "output-shaping-v1",
            "vision-budget-v1",
            "lean-prompt-v1",
        ),
        ("own_text_only",),
        ("near_match_code_prompt", "visual_small_detail"),
        ("changed_single_line", "required_detail_truncated"),
        _case_rules(
            ("own_text_only", "near_match_code_prompt", "visual_small_detail"),
            ("semantic-cache-v1", "decline_ineligible"),
            ("injected-efficiency-instructions-v1", "decline_ineligible"),
            ("injected-text-hard-compression-v1", "decline_ineligible"),
            ("output-shaping-v1", "decline_ineligible"),
            ("vision-budget-v1", "decline_ineligible"),
            ("lean-prompt-v1", "decline_ineligible"),
        )
        + _case_rules(
            ("changed_single_line", "required_detail_truncated"),
            ("semantic-cache-v1", "detect_corruption"),
            ("injected-efficiency-instructions-v1", "detect_corruption"),
            ("injected-text-hard-compression-v1", "detect_corruption"),
            ("output-shaping-v1", "detect_corruption"),
            ("vision-budget-v1", "detect_corruption"),
            ("lean-prompt-v1", "detect_corruption"),
        ),
        (
            "user_code_and_diagnostics_never_lossily_rewritten",
            "paired_outcome_required",
        ),
    ),
    Family(
        "protocol_and_isolation",
        "responses_protocol",
        ("all_enforceable_techniques",),
        (
            "stream_order",
            "retry_identity",
            "manual_continuation",
            "previous_response_continuation",
            "conversation_continuation",
        ),
        ("cancelled_stream", "401", "429", "500"),
        ("cross_tenant", "cross_principal", "cross_session", "unauthorized_artifact"),
        _case_rules(
            (
                "stream_order",
                "retry_identity",
                "manual_continuation",
                "previous_response_continuation",
                "conversation_continuation",
            ),
            ("all_enforceable_techniques", "no_material_opportunity"),
        )
        + _case_rules(
            ("cancelled_stream", "401", "429", "500"),
            ("all_enforceable_techniques", "decline_ineligible"),
        )
        + _case_rules(
            (
                "cross_tenant",
                "cross_principal",
                "cross_session",
                "unauthorized_artifact",
            ),
            ("all_enforceable_techniques", "reject_cross_scope_access"),
        ),
        ("sse_order_and_partial_terminal_state", "authorization_scope_exact"),
    ),
    Family(
        "ordinary_no_op_traffic",
        "ordinary_request",
        (
            "all_enforceable_techniques",
            "routing-context-profile-v1",
            "learned-soft-compression-v1",
            "speculative-decoding-v1",
        ),
        ("no_candidate",),
        ("candidate_not_smaller", "candidate_cache_harm"),
        ("unknown_dialect", "zero_retention"),
        _case_rules(
            ("no_candidate",),
            ("all_enforceable_techniques", "no_material_opportunity"),
            ("routing-context-profile-v1", "decline_ineligible"),
            ("learned-soft-compression-v1", "decline_ineligible"),
            ("speculative-decoding-v1", "no_material_opportunity"),
        )
        + _case_rules(
            ("candidate_not_smaller",),
            ("all_enforceable_techniques", "decline_framing_overhead"),
            ("routing-context-profile-v1", "decline_ineligible"),
            ("learned-soft-compression-v1", "decline_ineligible"),
            ("speculative-decoding-v1", "no_material_opportunity"),
        )
        + _case_rules(
            ("candidate_cache_harm",),
            ("all_enforceable_techniques", "decline_cache_harm"),
            ("routing-context-profile-v1", "decline_ineligible"),
            ("learned-soft-compression-v1", "decline_ineligible"),
            ("speculative-decoding-v1", "no_material_opportunity"),
        )
        + _case_rules(
            ("unknown_dialect", "zero_retention"),
            ("all_enforceable_techniques", "decline_ineligible"),
            ("routing-context-profile-v1", "decline_ineligible"),
            ("learned-soft-compression-v1", "decline_ineligible"),
            ("speculative-decoding-v1", "no_material_opportunity"),
        ),
        ("ordinary_traffic_byte_unchanged", "zero_retention_disables_recovery"),
    ),
)


def _scenario_data(
    family_id: str, case_id: str, oracle_class: str, record_id: str
) -> dict[str, Any]:
    common = {
        "case_id": case_id,
        "oracle_class": oracle_class,
        "synthetic_record_id": record_id,
    }
    if family_id == "repeated_lines":
        outputs = {
            "positive_run": "compile ok\n" * 12,
            "single_line": "compile ok\n",
            "count_not_smaller": "compile ok\ncompile ok\n",
            "mixed_endings": "compile ok\r\ncompile ok\n",
            "warning_output": "warning: synthetic fixture\n" * 3,
            "off_by_one_repeat_mutant": "compile ok\n" * 4,
        }
        output = outputs[case_id]
        repeated_line = "compile ok\n"
        repeat_count = output.count(repeated_line)
        rle_candidate = json.dumps(
            {"count": repeat_count, "line": repeated_line},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return common | {
            "output": output,
            "typed_exit_status": 0,
            "declared_repeat_count": 5
            if case_id == "off_by_one_repeat_mutant"
            else None,
            "actual_repeat_count": repeat_count,
            "rle_candidate_utf8": rle_candidate,
            "source_byte_count": len(output.encode("utf-8")),
            "rle_candidate_byte_count": len(rle_candidate.encode("utf-8")),
            "source_sha256": sha256_bytes(output.encode("utf-8")),
        }
    if family_id == "exact_and_partial_overlap":
        source_states = {
            "exact_visible_source": "visible",
            "missing_source": "missing",
            "evicted_source": "evicted",
            "below_70_percent_overlap": "visible",
            "cross_session_hash": "visible",
        }
        requester_scope = (
            "tenant-a/principal-a/session-b"
            if case_id == "cross_session_hash"
            else "tenant-a/principal-a/session-a"
        )
        large_visible_source = "".join(
            f"synthetic row {index:03d}: alpha beta gamma delta epsilon\n"
            for index in range(320)
        )
        short_visible_source = "synthetic row alpha\nsynthetic row beta\n"
        provider_visible_source = (
            (
                large_visible_source
                if case_id == "exact_visible_source"
                else short_visible_source
            )
            if source_states[case_id] == "visible"
            else None
        )
        if case_id == "exact_visible_source":
            new_results = [
                large_visible_source,
                large_visible_source.replace("epsilon", "zeta", 1),
            ]
        elif case_id == "below_70_percent_overlap":
            new_results = [
                "independent payload one\n",
                "zzzz qqqq xxxx\n",
            ]
        else:
            new_results = [
                short_visible_source,
                "synthetic row alpha\nsynthetic row gamma\n",
            ]
        target_candidate_index = 1 if case_id == "below_70_percent_overlap" else 0
        target_candidate = new_results[target_candidate_index]
        target_candidate_sha256 = sha256_bytes(target_candidate.encode("utf-8"))
        exact_duplicate_candidate = json.dumps(
            {
                "kind": "exact_provider_visible_reference",
                "scope": requester_scope,
                "sha256": target_candidate_sha256,
                "version": "exact-duplicate-reference-v1",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        source_chunks = (
            _fastcdc_chunks(provider_visible_source.encode("utf-8"))
            if provider_visible_source is not None
            else []
        )
        source_chunks_by_sha = {chunk["sha256"]: chunk for chunk in source_chunks}
        target_chunks = _fastcdc_chunks(target_candidate.encode("utf-8"))
        encoded_target_chunks: list[dict[str, Any]] = []
        referenced_byte_count = 0
        for chunk in target_chunks:
            target_bytes = target_candidate.encode("utf-8")[
                chunk["offset"] : chunk["offset"] + chunk["byte_count"]
            ]
            source_chunk = source_chunks_by_sha.get(chunk["sha256"])
            if source_chunk is not None:
                encoded_target_chunks.append(
                    {
                        "kind": "reference",
                        "sha256": chunk["sha256"],
                        "source_chunk_index": source_chunk["index"],
                    }
                )
                referenced_byte_count += chunk["byte_count"]
            else:
                encoded_target_chunks.append(
                    {
                        "kind": "literal_utf8",
                        "utf8": target_bytes.decode("utf-8"),
                    }
                )
        chunk_candidate = json.dumps(
            {
                "chunks": encoded_target_chunks,
                "scope": requester_scope,
                "version": "subresult-chunk-dedup-v1",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        sibling_map = []
        for sibling_index, sibling in enumerate(new_results):
            if sibling == provider_visible_source:
                pieces = [
                    {
                        "kind": "provider_visible_reference",
                        "sha256": sha256_bytes(sibling.encode("utf-8")),
                    }
                ]
            else:
                pieces = [{"kind": "literal_utf8", "utf8": sibling}]
            sibling_map.append({"index": sibling_index, "pieces": pieces})
        parallel_candidate = json.dumps(
            {
                "scope": requester_scope,
                "siblings": sibling_map,
                "version": "parallel-overlap-dedup-v1",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return common | {
            "source_state": source_states[case_id],
            "provider_visible_source": provider_visible_source,
            "new_results": new_results,
            "target_candidate_index": target_candidate_index,
            "target_candidate_sha256": target_candidate_sha256,
            "source_scope": "tenant-a/principal-a/session-a",
            "requester_scope": requester_scope,
            "overlap_ratio": (
                byte_overlap_ratio(provider_visible_source, target_candidate)
                if provider_visible_source is not None
                else None
            ),
            "source_artifact_recoverable": case_id
            not in {"missing_source", "evicted_source"},
            "reference_candidate_utf8": exact_duplicate_candidate,
            "exact_duplicate_candidate_utf8": exact_duplicate_candidate,
            "exact_duplicate_candidate_byte_count": len(
                exact_duplicate_candidate.encode("utf-8")
            ),
            "fastcdc_source_chunks": source_chunks,
            "fastcdc_target_chunks": target_chunks,
            "subresult_chunk_candidate_utf8": chunk_candidate,
            "subresult_chunk_candidate_byte_count": len(
                chunk_candidate.encode("utf-8")
            ),
            "content_defined_overlap_byte_count": referenced_byte_count,
            "content_defined_overlap_ratio": (
                referenced_byte_count / len(target_candidate.encode("utf-8"))
                if target_candidate
                else 0.0
            ),
            "parallel_overlap_candidate_utf8": parallel_candidate,
            "parallel_overlap_candidate_byte_count": len(
                parallel_candidate.encode("utf-8")
            ),
            "parallel_source_byte_count": sum(
                len(sibling.encode("utf-8")) for sibling in new_results
            ),
            "source_byte_count": len(provider_visible_source.encode("utf-8"))
            if provider_visible_source is not None
            else None,
            "target_candidate_byte_count": len(target_candidate.encode("utf-8")),
            "reference_candidate_byte_count": len(
                exact_duplicate_candidate.encode("utf-8")
            ),
            "content_defined_chunk_min_bytes": 512,
            "content_defined_chunk_target_bytes": 2048,
            "content_defined_chunk_max_bytes": 8192,
        }
    if family_id == "structured_json_and_tables":
        typed_json: Any = [
            {
                "row": index,
                "value": None if index % 3 == 0 else index,
                "code": f"{index:03d}" if index % 2 else str(index),
            }
            for index in range(1, 25)
        ]
        if case_id == "heterogeneous_rows":
            typed_json = [{"row": 1, "value": None}, {"row": "two", "extra": True}]
        source_json = json.dumps(typed_json, ensure_ascii=False, indent=2) + "\n"
        minified_json = json.dumps(
            typed_json, ensure_ascii=False, separators=(",", ":")
        )
        columns = list(typed_json[0])
        codec = {
            "columns": columns,
            "rows": [[row.get(column) for column in columns] for row in typed_json],
            "version": "ordered-table-v1",
        }
        codec_candidate = json.dumps(codec, ensure_ascii=False, separators=(",", ":"))
        return common | {
            "typed_json": typed_json,
            "raw_json": {
                "duplicate_keys": '{"row":1,"row":2}',
                "invalid_json": '{"row":',
                "changed_order_mutant": '{"columns":["b","a"]}',
            }.get(case_id),
            "required_semantics": {
                "ordered_rows": "row_order_preserved",
                "nulls": "null_distinct_from_missing",
                "numeric_vs_string": "numeric_and_string_types_distinct",
                "duplicate_keys": "decline_ambiguous_duplicate_keys",
                "heterogeneous_rows": "decline_heterogeneous_rows",
                "invalid_json": "detect_invalid_json",
                "changed_order_mutant": "detect_changed_order",
            }[case_id],
            "source_json_utf8": source_json,
            "json_minified_candidate_utf8": minified_json,
            "structured_codec_candidate_utf8": codec_candidate,
            "source_json_byte_count": len(source_json.encode("utf-8")),
            "json_minified_candidate_byte_count": len(minified_json.encode("utf-8")),
            "structured_codec_candidate_byte_count": len(
                codec_candidate.encode("utf-8")
            ),
        }
    if family_id == "terminal_and_progress":
        terminal_bytes = {
            "sgr": "\u001b[32mPASS\u001b[0m\n",
            "osc8": "\u001b]8;;https://fixture.invalid\u0007fixture link\u001b]8;;\u0007\n",
            "carriage_return_spinner": "step 1/2\rstep 2/2\n",
            "source_code_crlf": "value = 1\r\nvalue = 2\r\n",
            "signature_bytes": "signed:fixture-byte-sequence\n",
            "unknown_control": "prefix\u001b[?9999hsynthetic\n",
            "warning_output": "warning: synthetic fixture\n",
        }[case_id]
        transform_candidates = {
            "ansi-osc-strip-v1": {
                "sgr": "PASS\n",
                "osc8": "fixture link (https://fixture.invalid)\n",
            }.get(case_id),
            "trailing-noise-trim-v1": (
                "step 2/2\n" if case_id == "carriage_return_spinner" else None
            ),
        }
        return common | {
            "terminal_bytes_utf8": terminal_bytes,
            "content_kind": {
                "source_code_crlf": "source_code",
                "signature_bytes": "integrity_protected",
                "warning_output": "diagnostic",
            }.get(case_id, "terminal_display"),
            "known_control_sequence": case_id != "unknown_control",
            "transform_candidates_utf8": transform_candidates,
            "source_byte_count": len(terminal_bytes.encode("utf-8")),
            "transform_candidate_byte_counts": {
                technique_id: len(candidate.encode("utf-8"))
                if candidate is not None
                else None
                for technique_id, candidate in transform_candidates.items()
            },
        }
    if family_id == "file_rereads_and_diffs":
        base = "".join(
            f"setting_{index:03d} = {index:03d}  # synthetic stable fixture\n"
            for index in range(80)
        )
        small_edit = base.replace(
            "setting_042 = 042  # synthetic stable fixture\n",
            "setting_042 = 420  # synthetic reviewed edit\n",
        )
        current_by_case = {
            "unchanged": base,
            "small_edit": small_edit,
            "high_churn": "".join(
                f"replacement_{index:03d} = {index * 7:04d}\n" for index in range(80)
            ),
            "binary": "base64:AAECAwQ=",
            "stale_base": small_edit.replace("setting_001 = 001", "setting_001 = 999"),
            "edited_history_fork": small_edit.replace(
                "setting_043 = 043", "setting_043 = 777"
            ),
        }
        current = current_by_case[case_id]
        unified_diff = (
            "".join(
                difflib.unified_diff(
                    base.splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile="a/synthetic/module.py",
                    tofile="b/synthetic/module.py",
                )
            )
            if case_id != "binary"
            else None
        )
        current_byte_count = len(current.encode("utf-8"))
        patch_byte_count = (
            len(unified_diff.encode("utf-8")) if unified_diff is not None else None
        )
        path = (
            "synthetic/module.py" if case_id != "binary" else "synthetic/blob.bin"
        )
        current_sha256 = sha256_bytes(current.encode("utf-8"))
        repository_state = f"synthetic-tree-{sha256_bytes(base.encode('utf-8'))[:16]}"
        reread_receipt = {
            "schema": FILE_REREAD_RECEIPT_SCHEMA,
            "path": path,
            "repository_state": repository_state,
            "trusted_adapter": case_id in {"unchanged", "small_edit"},
            "encoding": "utf-8",
            "truncated": False,
            "signed": False,
            "byte_count": current_byte_count,
            "output_sha256": current_sha256,
        }
        unchanged_reference = json.dumps(
            {
                "artifact_sha256": current_sha256,
                "path": path,
                "scope": "tenant-a/principal-a/session-a",
                "version": "unchanged-file-identity-v1",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return common | {
            "path": path,
            "base": base,
            "current": current,
            "base_sha256": sha256_bytes(base.encode()),
            "current_sha256": current_sha256,
            "trusted_file_identity": case_id in {"unchanged", "small_edit"},
            "typed_reread_receipt": reread_receipt,
            "provider_visible_source_utf8": base,
            "provider_visible_source_sha256": sha256_bytes(base.encode("utf-8")),
            "provider_visible_source_byte_count": len(base.encode("utf-8")),
            "provider_visible_source_scope": "tenant-a/principal-a/session-a",
            "requester_scope": "tenant-a/principal-a/session-a",
            "unchanged_reference_candidate_utf8": unchanged_reference,
            "unchanged_reference_candidate_byte_count": len(
                unchanged_reference.encode("utf-8")
            ),
            "artifact_expansion_utf8": current,
            "repository_state": repository_state,
            "binary": case_id == "binary",
            "history_state": (
                "stale_base"
                if case_id == "stale_base"
                else "forked"
                if case_id == "edited_history_fork"
                else "current"
            ),
            "unified_diff_utf8": unified_diff,
            "base_byte_count": len(base.encode("utf-8")),
            "current_byte_count": current_byte_count,
            "patch_byte_count": patch_byte_count,
            "patch_to_current_ratio": (
                patch_byte_count / current_byte_count
                if patch_byte_count is not None and current_byte_count
                else None
            ),
        }
    if family_id == "commands_checks_and_logs":
        lines = [f"case_{index:03d}: passed" for index in range(84)]
        if case_id in {"failed_check", "warning_on_success", "log_output"}:
            lines.append(
                "failure: synthetic assertion"
                if case_id == "failed_check"
                else "warning: synthetic boundary signal"
            )
        if case_id == "oversized":
            materialized_lines = ["x" * MAX_COMMAND_OUTPUT_BYTES]
            output_utf8 = materialized_lines[0] + "\n"
        else:
            materialized_lines = lines
            output_utf8 = "\n".join(materialized_lines) + "\n"
        output_sha256 = sha256_bytes(output_utf8.encode("utf-8"))
        projection_version = (
            "commands-unknown-v9" if case_id == "unknown_version" else "commands-v1"
        )
        projection_value = {
            "artifact_sha256": output_sha256,
            "command": "python3 -m unittest synthetic_fixture_test",
            "exit_status": {"failed_check": 1, "ambiguous_exit_status": None}.get(
                case_id, 0
            ),
            "head": materialized_lines[:2],
            "tail": materialized_lines[-2:],
            "version": projection_version,
        }
        projection = json.dumps(
            projection_value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if case_id in {"truncated", "oversized", "log_output"}:
            projection = None
        transform_case = case_id in {
            "successful_long_check",
            "test_output",
            "build_output",
        }
        return common | {
            "command": "python3 -m unittest synthetic_fixture_test",
            "output_kind": {
                "test_output": "test",
                "build_output": "build",
                "git_status": "git_status",
                "git_diff": "git_diff",
                "log_output": "log",
            }.get(case_id, "check"),
            "exit_status": {
                "failed_check": 1,
                "ambiguous_exit_status": None,
            }.get(case_id, 0),
            "warning_present": case_id == "warning_on_success",
            "truncated": case_id == "truncated",
            "oversized": case_id == "oversized",
            "projection_version": projection_version,
            "lines": materialized_lines,
            "output_utf8": output_utf8,
            "output_sha256": output_sha256,
            "output_byte_count": len(output_utf8.encode("utf-8")),
            "output_line_count": len(materialized_lines),
            "projection_candidate_utf8": projection,
            "projection_candidate_byte_count": (
                len(projection.encode("utf-8")) if projection is not None else None
            ),
            "artifact_expansion_utf8": output_utf8 if transform_case else None,
            "passthrough_required": case_id
            in {"unknown_version", "truncated", "oversized", "log_output"},
            "maximum_eligible_output_bytes": MAX_COMMAND_OUTPUT_BYTES,
        }
    if family_id == "cache_sensitive_prefixes":
        condition = {
            "cold": "cold",
            "warm_same_prefix": "warm_identical_prefix",
            "warm_changed_suffix": "warm_changed_suffix",
            "expired": "expired",
            "smaller_but_more_expensive": "warm_identical_prefix",
            "missing_cache_fields": "usage_fields_missing",
        }[case_id]
        prefix = ["fixed system fixture", "fixed tool schema fixture"]
        suffix = (
            "synthetic changed suffix"
            if case_id == "warm_changed_suffix"
            else "synthetic stable suffix"
        )
        provider_visible_prefix = "\n".join(prefix) + "\n"
        return common | {
            "prefix": prefix,
            "suffix": suffix,
            "cache_condition": condition,
            "provider_cached_tokens": {
                "cold": 0,
                "warm_same_prefix": 128,
                "warm_changed_suffix": 64,
                "expired": 0,
                "smaller_but_more_expensive": 128,
                "missing_cache_fields": None,
            }[case_id],
            "cache_fields_present": case_id != "missing_cache_fields",
            "cache_entry_expired": case_id == "expired",
            "projected_original_cost_microusd": 40,
            "projected_candidate_cost_microusd": 45
            if case_id in {"warm_changed_suffix", "smaller_but_more_expensive"}
            else 30,
            "candidate_smaller": case_id != "missing_cache_fields",
            "provider_visible_prefix_utf8": provider_visible_prefix,
            "replayed_prefix_utf8": provider_visible_prefix,
            "candidate_suffix_utf8": suffix,
            "provider_visible_prefix_byte_count": len(
                provider_visible_prefix.encode("utf-8")
            ),
            "candidate_suffix_byte_count": len(suffix.encode("utf-8")),
        }
    if family_id == "cooperative_context":
        return common | {
            "context_epoch": "fixture-epoch-1"
            if case_id not in {"no_cloud_seam", "failed_expansion"}
            else None,
            "events": (
                ["tool_b.started", "tool_a.completed"]
                if case_id == "changed_order_mutant"
                else [
                    "tool_a.started",
                    "tool_a.completed",
                    "tool_b.started",
                    "tool_b.completed",
                ]
            ),
            "expand_artifact_available": case_id
            not in {"no_cloud_seam", "failed_expansion"},
            "tool_search_available": case_id != "tool_discovery_needed",
            "candidate_count": 0 if case_id == "no_op_poll" else 1,
            "batch_order_frozen": case_id == "ordered_batch",
        }
    if family_id == "behavioral_and_lossy":
        return common | {
            "owner": "compand_fixture"
            if case_id == "own_text_only"
            else "user_fixture",
            "text": "Synthetic guidance: avoid repeating already-visible fixture output.",
            "near_match": case_id == "near_match_code_prompt",
            "single_line_changed": case_id == "changed_single_line",
            "required_detail": "Preserve the exact synthetic line number 17.",
            "required_detail_present": case_id != "required_detail_truncated",
            "image_descriptor": "generated 8x8 checkerboard with one marked corner",
            "small_visual_detail_required": case_id == "visual_small_detail",
        }
    if family_id == "protocol_and_isolation":
        status = {"401": 401, "429": 429, "500": 500}.get(
            case_id,
            403
            if case_id
            in {
                "cross_tenant",
                "cross_principal",
                "cross_session",
                "unauthorized_artifact",
            }
            else 200,
        )
        requested_scope = {
            "tenant": "fixture-tenant-b"
            if case_id == "cross_tenant"
            else "fixture-tenant-a",
            "principal": "fixture-principal-b"
            if case_id == "cross_principal"
            else "fixture-principal-a",
            "session": "fixture-session-b"
            if case_id == "cross_session"
            else "fixture-session-protocol",
        }
        continuation_mode = {
            "manual_continuation": "manual_replay",
            "previous_response_continuation": "previous_response_id",
            "conversation_continuation": "conversation_id",
        }.get(case_id)
        frames = [
            "response.created",
            "response.output_text.delta",
            "response.completed",
        ]
        if case_id == "cancelled_stream":
            provider_body = CANCELLATION_FIXTURE_PATH.read_bytes().decode("utf-8")
            sse_frames = _split_sse_frames(provider_body)
            frames = [frame.splitlines()[0].removeprefix("event: ") for frame in sse_frames]
        elif status != 200:
            frames = []
            sse_frames = []
        else:
            sse_frames = [
                f"event: {event}\ndata: "
                + json.dumps(
                    {"fixture": record_id, "type": event},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n\n"
                for event in frames
            ]
        provider_error_bodies = {
            "401": '{"error":{"code":"invalid_api_key","message":"synthetic unauthorized","type":"authentication_error"}}',
            "429": '{"error":{"code":"rate_limit_exceeded","message":"synthetic rate limit","type":"rate_limit_error"}}',
            "500": '{"error":{"code":"internal_error","message":"synthetic provider failure","type":"server_error"}}',
        }
        if case_id != "cancelled_stream":
            provider_body = provider_error_bodies.get(case_id, "".join(sse_frames))
        terminal_events = {
            "response.completed",
            "response.cancelled",
            "response.failed",
            "response.incomplete",
        }
        return common | {
            "frames": frames,
            "sse_frames_utf8": sse_frames,
            "stream_cancelled": case_id == "cancelled_stream",
            "status": status,
            "continuation_mode": continuation_mode,
            "previous_response_id": "resp_fixture_previous"
            if continuation_mode == "previous_response_id"
            else None,
            "conversation_id": "conv_fixture"
            if continuation_mode == "conversation_id"
            else None,
            "attempts": (
                [
                    {
                        "attempt": 1,
                        "provider_dispatched": False,
                        "usage_recorded": False,
                        "request_identity": "retry-fixture-1",
                    },
                    {
                        "attempt": 2,
                        "provider_dispatched": True,
                        "usage_recorded": True,
                        "request_identity": "retry-fixture-1",
                    },
                ]
                if case_id == "retry_identity"
                else [
                    {
                        "attempt": 1,
                        "provider_dispatched": status in {200, 429, 500},
                        "usage_recorded": status == 200,
                        "request_identity": record_id,
                    }
                ]
            ),
            "requested_scope": requested_scope,
            "authorized_scope": {
                "tenant": "fixture-tenant-a",
                "principal": "fixture-principal-a",
                "session": "fixture-session-protocol",
            },
            "artifact_authorized": case_id != "unauthorized_artifact",
            "provider_response_body_utf8": provider_body,
            "provider_response_body_byte_count": len(provider_body.encode("utf-8")),
            "provider_response_body_sha256": sha256_bytes(
                provider_body.encode("utf-8")
            ),
            "frozen_source_path": (
                str(CANCELLATION_FIXTURE_PATH.relative_to(ROOT))
                if case_id == "cancelled_stream"
                else None
            ),
            "terminal_event_present": any(event in terminal_events for event in frames),
            "usage_present": False if case_id == "cancelled_stream" else status == 200,
            "sse_frame_byte_counts": [
                len(frame.encode("utf-8")) for frame in sse_frames
            ],
            "sse_frame_sha256s": [
                sha256_bytes(frame.encode("utf-8")) for frame in sse_frames
            ],
            "passthrough_required": case_id
            in {"cancelled_stream", "401", "429", "500"},
        }
    return common | {
        "candidate_count": 0 if case_id in {"no_candidate", "zero_retention"} else 1,
        "candidate_smaller": case_id == "candidate_cache_harm",
        "candidate_cache_harm": case_id == "candidate_cache_harm",
        "dialect": "unknown-fixture-v9"
        if case_id == "unknown_dialect"
        else "responses-fixture-v1",
        "retention": "zero" if case_id == "zero_retention" else "session",
    }


def _expanded_dispositions(
    technique_dispositions: tuple[tuple[str, str], ...], enforceable_ids: set[str]
) -> dict[str, str]:
    expanded: dict[str, str] = {}
    for technique, disposition in technique_dispositions:
        if technique == "all_enforceable_techniques":
            for technique_id in sorted(enforceable_ids):
                expanded[technique_id] = disposition
        else:
            if technique in expanded:
                raise ValueError(
                    f"duplicate disposition for explicitly named technique {technique}"
                )
            expanded[technique] = disposition
    return expanded


def _case_dispositions(
    family: Family,
    case_id: str,
    enforceable_ids: set[str],
) -> dict[str, str]:
    matches = [
        technique_dispositions
        for candidate, technique_dispositions in family.dispositions_by_case
        if candidate == case_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{family.family_id}:{case_id} must declare exactly one disposition map"
        )
    return _expanded_dispositions(matches[0], enforceable_ids)


def _baseline_outcome(
    family_id: str, case_id: str, scenario: dict[str, Any]
) -> dict[str, Any]:
    if family_id == "protocol_and_isolation" and case_id == "cancelled_stream":
        return {
            "status": "cancelled",
            "verified_task_success": False,
            "provider_status": 200,
            "retry_allowed": False,
            "passthrough_required": True,
            "provider_body_byte_count": scenario["provider_response_body_byte_count"],
            "provider_body_sha256": scenario["provider_response_body_sha256"],
            "sse_frame_byte_counts": scenario["sse_frame_byte_counts"],
            "sse_frame_sha256s": scenario["sse_frame_sha256s"],
            "terminal_event_present": scenario["terminal_event_present"],
            "usage_present": scenario["usage_present"],
        }
    if family_id == "protocol_and_isolation" and case_id in {"401", "429", "500"}:
        return {
            "status": "provider_error",
            "verified_task_success": False,
            "provider_status": int(case_id),
            "retry_allowed": False,
            "passthrough_required": True,
            "provider_body_byte_count": scenario["provider_response_body_byte_count"],
            "provider_body_sha256": scenario["provider_response_body_sha256"],
            "sse_frame_byte_counts": [],
            "sse_frame_sha256s": [],
        }
    if family_id == "protocol_and_isolation" and case_id in {
        "cross_tenant",
        "cross_principal",
        "cross_session",
        "unauthorized_artifact",
    }:
        return {
            "status": "authorization_rejected",
            "verified_task_success": False,
            "provider_status": 403,
            "retry_allowed": False,
        }
    if family_id == "protocol_and_isolation" and case_id == "retry_identity":
        return {
            "status": "verified_completed_after_infrastructure_retry",
            "verified_task_success": True,
            "provider_status": 200,
            "retry_allowed": True,
            "retry_limit": 1,
            "request_identity_preserved": True,
        }
    return {
        "status": "verified_completed",
        "verified_task_success": True,
        "provider_status": 200,
        "retry_allowed": False,
    }


def _case_record(
    family: Family,
    oracle_class: str,
    case_id: str,
    partition: str,
    fixture_variant: str,
    enforceable_ids: set[str],
) -> dict[str, Any]:
    record_id = (
        f"{family.family_id}-{partition}-{fixture_variant}-{oracle_class}-{case_id}-v1"
    )
    scenario = _scenario_data(family.family_id, case_id, oracle_class, record_id)
    call_id = f"call-{record_id}"
    provider_output = (
        scenario["output_utf8"]
        if family.family_id == "commands_checks_and_logs"
        else json.dumps(scenario, ensure_ascii=False, sort_keys=True)
    )
    if family.family_id == "commands_checks_and_logs":
        transform_case = case_id in {
            "successful_long_check",
            "test_output",
            "build_output",
        }
        scenario["typed_command_result_receipt"] = {
            "schema": COMMAND_RECEIPT_SCHEMA,
            "call_id": call_id,
            "source_kind": (
                "log_result" if case_id == "log_output" else "command_result"
            ),
            "trusted_adapter": True,
            "exit_status": scenario["exit_status"],
            "content_type": "text/plain",
            "encoding": "utf-8",
            "truncated": scenario["truncated"],
            "signed": False,
            "byte_count": len(provider_output.encode("utf-8")),
            "output_sha256": sha256_bytes(provider_output.encode("utf-8")),
            "output_utf8": provider_output if transform_case else None,
            "new_suffix": True,
            "expected_eligible": transform_case,
        }
    scope = {
        "tenant": "fixture-tenant-a",
        "principal": "fixture-principal-a",
        "session": f"fixture-session-{family.family_id}",
        "context_epoch": "fixture-epoch-1",
    }
    requested_scope = scenario.get("requested_scope")
    if isinstance(requested_scope, dict):
        scope.update(requested_scope)
    provider_request = {
        "method": "POST",
        "path": "/v1/responses",
        "headers": {"content-type": "application/json", "x-fixture-auth": "dummy"},
        "body": {
            "model": "fixture-model",
            "store": False,
            "stream": family.family_id == "protocol_and_isolation",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": provider_output,
                }
            ],
        },
    }
    fixture_input = {
        "schema": "compand.ces1.case_input.v1",
        "scope": scope,
        "scenario": scenario,
        "provider_request": provider_request,
    }
    disposition_map = _case_dispositions(family, case_id, enforceable_ids)
    expected_outcome = _baseline_outcome(family.family_id, case_id, scenario)
    return {
        "schema": CASE_SCHEMA,
        "record_id": record_id,
        "case_id": case_id,
        "oracle_class": oracle_class,
        "input_sha256": sha256_json(fixture_input),
        "baseline_provider_view_sha256": sha256_json(provider_request),
        "expected_outcome": expected_outcome,
        "eligible_techniques": sorted(
            technique_id
            for technique_id, disposition in disposition_map.items()
            if technique_id in enforceable_ids and disposition == "transform"
        ),
        "expected_disposition_by_technique": disposition_map,
        "required_invariants": [
            "baseline_provider_view_is_byte_unchanged_B0",
            "baseline_input_and_provider_view_hashes_match",
            "tenant_principal_session_context_epoch_isolation",
            "source_bytes_are_never_overwritten",
            *family.invariants,
        ],
        "input": fixture_input,
        "baseline_provider_view": provider_request,
    }


def _fixture(
    family: Family,
    fixture_variant: str,
    partition: str,
    case_groups: tuple[tuple[str, tuple[str, ...]], ...],
    enforceable_ids: set[str],
) -> dict[str, Any]:
    fixture_id = f"{family.family_id}-{fixture_variant}-v1"
    case_records = [
        _case_record(
            family,
            oracle_class,
            case_id,
            partition,
            fixture_variant,
            enforceable_ids,
        )
        for oracle_class, cases in case_groups
        for case_id in cases
    ]
    fixture_input = {
        "schema": "compand.ces1.fixture_input_bundle.v1",
        "case_input_hashes": [
            {
                "record_id": record["record_id"],
                "input_sha256": record["input_sha256"],
            }
            for record in case_records
        ],
    }
    baseline_provider_view = {
        "schema": "compand.ces1.provider_view_bundle.v1",
        "case_provider_view_hashes": [
            {
                "record_id": record["record_id"],
                "baseline_provider_view_sha256": record[
                    "baseline_provider_view_sha256"
                ],
            }
            for record in case_records
        ],
    }
    expected_outcome = {
        "schema": "compand.ces1.case_outcome_bundle.v1",
        "by_record_id": {
            record["record_id"]: record["expected_outcome"] for record in case_records
        },
    }
    disposition_summary: dict[str, list[str]] = {}
    for record in case_records:
        for technique_id, disposition in record[
            "expected_disposition_by_technique"
        ].items():
            disposition_summary.setdefault(technique_id, []).append(disposition)
    disposition_summary = {
        technique_id: sorted(set(dispositions))
        for technique_id, dispositions in sorted(disposition_summary.items())
    }
    return {
        "schema": SCHEMA,
        "fixture_id": fixture_id,
        "revision": 5,
        "partition": partition,
        "fixture_family": family.family_id,
        "cases": [record["case_id"] for record in case_records],
        "oracle_classes": sorted({record["oracle_class"] for record in case_records}),
        "case_record_count": len(case_records),
        "case_records": case_records,
        "workload_stratum": family.workload_stratum,
        "input_sha256": sha256_json(fixture_input),
        "baseline_provider_view_sha256": sha256_json(baseline_provider_view),
        "expected_outcome": expected_outcome,
        "eligible_techniques": sorted(
            {
                technique_id
                for record in case_records
                for technique_id in record["eligible_techniques"]
            }
        ),
        "expected_disposition_by_technique": disposition_summary,
        "required_invariants": [
            "baseline_provider_view_is_byte_unchanged_B0",
            "baseline_input_and_provider_view_hashes_match",
            "tenant_principal_session_context_epoch_isolation",
            "source_bytes_are_never_overwritten",
            *family.invariants,
        ],
        "license": {"spdx": "CC0-1.0", "reviewed": True},
        "generator_or_source": {
            "kind": "sanitized_synthetic",
            "generator": "scripts/build_compand_phase2_corpus.py",
            "raw_customer_content": False,
        },
        "input": fixture_input,
        "baseline_provider_view": baseline_provider_view,
    }


def _holdout_plan() -> dict[str, Any]:
    return {
        "schema": "compand.ces1.hidden_holdout_plan.v1",
        "version": VERSION,
        "status": "reserved_pending_independent_custody",
        "developer_visible": False,
        "payloads_in_repository": False,
        "custodian": "independent_QA-58_holdout_custodian",
        "attested": False,
        "required_commitments": [
            "input_sha256",
            "baseline_provider_view_sha256",
            "expected_oracle_sha256",
            "encrypted_bundle_sha256",
            "custodian_attestation",
        ],
        "reservations": [
            {
                "reservation_id": f"hidden-{family.family_id}-slot-v1",
                "fixture_family": family.family_id,
                "partition": "hidden_holdout",
                "workload_stratum": family.workload_stratum,
                "case_count_minimum": 1,
                "generator_version": VERSION,
                "license_required": True,
                "contamination_check_required": True,
            }
            for family in FAMILIES
        ],
    }


def _partition_root(entries: list[dict[str, Any]]) -> str:
    return sha256_json(
        [{"path": entry["path"], "sha256": entry["sha256"]} for entry in entries]
    )


def build_outputs() -> dict[Path, bytes]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    enforceable_ids = {
        item["id"]
        for item in catalog["techniques"]
        if item["cloud_gateway_enforceable"]
    }
    outputs: dict[Path, bytes] = {}
    entries_by_partition: dict[str, list[dict[str, Any]]] = {
        "development": [],
        "golden": [],
    }
    visible_outcomes: list[dict[str, Any]] = []
    variants = (
        (
            "positive",
            "development",
            (("positive", "positive_cases"),),
        ),
        (
            "boundary",
            "development",
            (("boundary", "boundary_cases"),),
        ),
        (
            "golden",
            "golden",
            (
                ("positive", "positive_cases"),
                ("negative", "negative_cases"),
            ),
        ),
    )
    for family in FAMILIES:
        for fixture_variant, partition, group_names in variants:
            case_groups = tuple(
                (
                    oracle_class,
                    getattr(family, cases_name),
                )
                for oracle_class, cases_name in group_names
            )
            fixture = _fixture(
                family,
                fixture_variant,
                partition,
                case_groups,
                enforceable_ids,
            )
            relative = Path("visible") / partition / f"{fixture['fixture_id']}.json"
            content = pretty_bytes(fixture)
            outputs[CORPUS_ROOT / relative] = content
            entry = {
                "fixture_id": fixture["fixture_id"],
                "family": family.family_id,
                "path": relative.as_posix(),
                "sha256": sha256_bytes(content),
                "input_sha256": fixture["input_sha256"],
                "baseline_provider_view_sha256": fixture[
                    "baseline_provider_view_sha256"
                ],
                "case_record_count": fixture["case_record_count"],
                "oracle_classes": fixture["oracle_classes"],
            }
            entries_by_partition[partition].append(entry)
            visible_outcomes.extend(
                {
                    "record_id": record["record_id"],
                    "expected_outcome": record["expected_outcome"],
                }
                for record in fixture["case_records"]
            )

    holdout_plan = _holdout_plan()
    holdout_content = pretty_bytes(holdout_plan)
    outputs[CORPUS_ROOT / "hidden-holdout-plan.json"] = holdout_content
    holdout_plan_sha = sha256_bytes(holdout_content)
    visible_outcomes.sort(key=lambda item: item["record_id"])
    outcome_hash = sha256_json(visible_outcomes)
    development_root = _partition_root(entries_by_partition["development"])
    golden_root = _partition_root(entries_by_partition["golden"])
    corpus_root = sha256_json(
        {
            "version": VERSION,
            "development_root_sha256": development_root,
            "golden_root_sha256": golden_root,
            "hidden_holdout_plan_sha256": holdout_plan_sha,
            "baseline_outcome_sha256": outcome_hash,
        }
    )
    index = {
        "schema": "compand.ces1.corpus_index.v1",
        "corpus_id": "compand-phase2-technique-corpus",
        "version": VERSION,
        "status": "visible_materialized_hidden_reserved",
        "corpus_root_sha256": corpus_root,
        "fixture_count": sum(len(items) for items in entries_by_partition.values()),
        "case_record_count": sum(
            entry["case_record_count"]
            for items in entries_by_partition.values()
            for entry in items
        ),
        "partitions": {
            "development": {
                "fixture_count": len(entries_by_partition["development"]),
                "case_record_count": sum(
                    entry["case_record_count"]
                    for entry in entries_by_partition["development"]
                ),
                "root_sha256": development_root,
                "fixtures": entries_by_partition["development"],
            },
            "golden": {
                "fixture_count": len(entries_by_partition["golden"]),
                "case_record_count": sum(
                    entry["case_record_count"]
                    for entry in entries_by_partition["golden"]
                ),
                "root_sha256": golden_root,
                "fixtures": entries_by_partition["golden"],
            },
            "hidden_holdout": {
                "fixture_count": 0,
                "case_record_count": 0,
                "reservation_count": len(holdout_plan["reservations"]),
                "root_sha256": None,
                "plan_sha256": holdout_plan_sha,
                "attested": False,
            },
        },
        "baseline_task_outcome_sha256": outcome_hash,
        "immutability": {
            "algorithm": "sha256",
            "canonical_json": "UTF-8 sorted keys compact separators",
            "corrections": "new_version_only",
        },
    }
    index_content = pretty_bytes(index)
    outputs[CORPUS_ROOT / "index.json"] = index_content

    outputs[
        CORPUS_ROOT / "README.md"
    ] = f"""# Compand Phase 2 technique corpus {VERSION}

This directory is generated by `python3 scripts/build_compand_phase2_corpus.py
--write` and verified by running the same command without `--write`.

- `visible/development/` contains sanitized synthetic plugin-development case records.
- `visible/golden/` contains frozen positive and negative per-case transform oracles.
- Every declared case has its own hashed input, provider view, expected outcome,
  technique disposition map, and invariant set; fixture-level fields are bundle hashes.
- `hidden-holdout-plan.json` freezes one opaque reservation per fixture family.
  It intentionally contains no payload or expected oracle. The independent QA-58
  custodian must attest encrypted payload commitments before the hidden partition
  receives a root hash or this corpus can be confirmatory-ready.

All visible provider requests are dummy, local fixtures. No customer content or real
credential is present. `index.json`, `CHECKSUMS`, and the corpus manifest pin the
visible bytes and baseline task outcomes. Corrections require a new corpus version.
""".encode("utf-8")

    checksums = []
    for path, content in sorted(outputs.items(), key=lambda item: item[0].as_posix()):
        relative = path.relative_to(CORPUS_ROOT).as_posix()
        if relative != "CHECKSUMS":
            checksums.append(f"{sha256_bytes(content)}  {relative}")
    outputs[CORPUS_ROOT / "CHECKSUMS"] = ("\n".join(checksums) + "\n").encode()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["version"] = VERSION
    manifest["status"] = "visible_materialized_hidden_reserved"
    required_fields = manifest["fixture_contract"]["required_fields"]
    for field in ("case_record_count", "case_records"):
        if field not in required_fields:
            required_fields.append(field)
    manifest["fixture_contract"]["fixture_schema"] = SCHEMA
    manifest["fixture_contract"]["case_record_contract"] = {
        "schema": CASE_SCHEMA,
        "authority": "case_records are authoritative; fixture-level fields are bundle summaries",
        "required_fields": [
            "record_id",
            "case_id",
            "oracle_class",
            "input_sha256",
            "baseline_provider_view_sha256",
            "expected_outcome",
            "eligible_techniques",
            "expected_disposition_by_technique",
            "required_invariants",
            "input",
            "baseline_provider_view",
        ],
    }
    manifest["materialization"] = {
        "corpus_root_sha256": corpus_root,
        "manifest_sha256": None,
        "visible_fixture_count": index["fixture_count"],
        "visible_case_record_count": index["case_record_count"],
        "hidden_holdout_fixture_count": 0,
        "hidden_holdout_reservation_count": len(holdout_plan["reservations"]),
        "fixture_count": index["fixture_count"],
        "baseline_task_outcome_sha256": outcome_hash,
        "confirmatory_ready": False,
        "blocking_reasons": [
            "hidden_holdout_payload_commitments_not_yet_attested_by_QA-58_custodian"
        ],
        "manifest_hash_rule": "sha256 of canonical JSON with materialization.manifest_sha256 set to null",
    }
    roots = {
        "development": development_root,
        "golden": golden_root,
        "hidden_holdout": None,
    }
    for partition in manifest["partitions"]:
        partition["root_sha256"] = roots[partition["id"]]
        if partition["id"] == "development":
            partition["fixture_count"] = len(entries_by_partition["development"])
            partition["case_record_count"] = index["partitions"]["development"][
                "case_record_count"
            ]
        elif partition["id"] == "golden":
            partition["fixture_count"] = len(entries_by_partition["golden"])
            partition["case_record_count"] = index["partitions"]["golden"][
                "case_record_count"
            ]
        else:
            partition["fixture_count"] = 0
            partition["case_record_count"] = 0
            partition["reservation_count"] = len(holdout_plan["reservations"])
            partition["plan_sha256"] = holdout_plan_sha
            partition["custody_status"] = "pending_independent_attestation"
    manifest["task_outcomes"] = {
        "oracle_version": "qa56-baseline-oracle-v1",
        "baseline_outcome_hash": outcome_hash,
        "quality_metric": "verified_task_success",
        "noninferiority_margin_absolute": -0.05,
        "blinded_evaluator_required": True,
    }
    manifest["materialization"]["manifest_sha256"] = sha256_json(manifest)
    outputs[MANIFEST_PATH] = pretty_bytes(manifest)
    return outputs


def managed_paths() -> set[Path]:
    paths = {MANIFEST_PATH}
    if CORPUS_ROOT.exists():
        paths.update(path for path in CORPUS_ROOT.rglob("*") if path.is_file())
    return paths


def verify(outputs: dict[Path, bytes]) -> list[str]:
    errors: list[str] = []
    expected_paths = set(outputs)
    for path, expected in outputs.items():
        if not path.is_file():
            errors.append(f"missing {path.relative_to(ROOT)}")
        elif path.read_bytes() != expected:
            errors.append(f"drift {path.relative_to(ROOT)}")
    for path in sorted(managed_paths() - expected_paths):
        errors.append(f"unexpected {path.relative_to(ROOT)}")
    return errors


def write(outputs: dict[Path, bytes]) -> None:
    for path in sorted(managed_paths() - set(outputs), reverse=True):
        path.unlink()
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write", action="store_true", help="regenerate committed files"
    )
    args = parser.parse_args()
    outputs = build_outputs()
    if args.write:
        write(outputs)
        outputs = build_outputs()
    errors = verify(outputs)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"verified {len(outputs)} QA-56 corpus files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
