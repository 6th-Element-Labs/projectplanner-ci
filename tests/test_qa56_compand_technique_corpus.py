"""QA-56: immutable sanitized Compand corpus and baseline-oracle checks."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from path_setup import ROOT


CORPUS = ROOT / "fixtures" / "compand" / "phase2-technique-corpus" / "v1"
CONTRACT = ROOT / "docs" / "compand" / "phase2"
GENERATOR = ROOT / "scripts" / "build_compand_phase2_corpus.py"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_bytes(value))


def independent_fastcdc_chunks(
    data: bytes, minimum: int = 512, target: int = 2048, maximum: int = 8192
) -> list[dict]:
    mask = target - 1
    gear = [
        int.from_bytes(hashlib.sha256(bytes([value])).digest()[:8], "big")
        for value in range(256)
    ]
    chunks = []
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


def split_sse_frames(body: str) -> list[str]:
    frames = []
    cursor = 0
    while cursor < len(body):
        boundary = body.find("\n\n", cursor)
        if boundary < 0:
            frames.append(body[cursor:])
            break
        frames.append(body[cursor : boundary + 2])
        cursor = boundary + 2
    return frames


# This is an independent frozen oracle.  It deliberately does not import the corpus
# generator's FAMILIES table or derive expected values from generated fixture files.
FROZEN_ENFORCEABLE_TECHNIQUES = (
    "ansi-osc-strip-v1",
    "command-aware-projection-v1",
    "delta-reread-v1",
    "exact-duplicate-reference-v1",
    "json-minify-v1",
    "line-ending-normalize-v1",
    "line-rle-v1",
    "parallel-overlap-dedup-v1",
    "prefix-cache-shaping-v1",
    "structured-data-codec-v1",
    "subresult-chunk-dedup-v1",
    "successful-check-projection-v1",
    "trailing-noise-trim-v1",
    "unchanged-file-identity-v1",
)
REPEATED_TECHNIQUES = ("line-rle-v1",)
OVERLAP_TECHNIQUES = (
    "exact-duplicate-reference-v1",
    "subresult-chunk-dedup-v1",
    "parallel-overlap-dedup-v1",
)
STRUCTURED_TECHNIQUES = ("json-minify-v1", "structured-data-codec-v1")
TERMINAL_TECHNIQUES = (
    "ansi-osc-strip-v1",
    "line-ending-normalize-v1",
    "trailing-noise-trim-v1",
)
FILE_TECHNIQUES = ("unchanged-file-identity-v1", "delta-reread-v1")
COMMAND_TECHNIQUES = (
    "command-aware-projection-v1",
    "successful-check-projection-v1",
)
CACHE_TECHNIQUES = (
    "prefix-cache-shaping-v1",
    "provider-kv-reuse-v1",
    "transport-gzip-v1",
)
COOPERATIVE_TECHNIQUES = (
    "context-paging-v1",
    "schema-deferral-v1",
    "turn-elimination-v1",
    "agent-memory-summary-v1",
    "code-action-batching-v1",
)
BEHAVIORAL_TECHNIQUES = (
    "semantic-cache-v1",
    "injected-efficiency-instructions-v1",
    "injected-text-hard-compression-v1",
    "output-shaping-v1",
    "vision-budget-v1",
    "lean-prompt-v1",
)


def _uniform(techniques: tuple[str, ...], disposition: str) -> dict[str, str]:
    return {technique: disposition for technique in techniques}


EXPECTED_DISPOSITIONS_BY_CASE: dict[tuple[str, str], dict[str, str]] = {}


def _expect(
    family: str,
    cases: tuple[str, ...],
    dispositions: dict[str, str],
) -> None:
    for case_id in cases:
        key = (family, case_id)
        if key in EXPECTED_DISPOSITIONS_BY_CASE:
            raise AssertionError(f"duplicate independent disposition oracle: {key}")
        EXPECTED_DISPOSITIONS_BY_CASE[key] = dict(dispositions)


_expect("repeated_lines", ("positive_run",), _uniform(REPEATED_TECHNIQUES, "transform"))
_expect(
    "repeated_lines",
    ("single_line", "count_not_smaller"),
    _uniform(REPEATED_TECHNIQUES, "decline_framing_overhead"),
)
_expect(
    "repeated_lines",
    ("mixed_endings", "warning_output", "off_by_one_repeat_mutant"),
    _uniform(REPEATED_TECHNIQUES, "detect_corruption"),
)

_expect(
    "exact_and_partial_overlap",
    ("exact_visible_source",),
    _uniform(OVERLAP_TECHNIQUES, "transform"),
)
_expect(
    "exact_and_partial_overlap",
    ("missing_source", "evicted_source"),
    _uniform(OVERLAP_TECHNIQUES, "fail_recovery"),
)
_expect(
    "exact_and_partial_overlap",
    ("below_70_percent_overlap",),
    {
        "exact-duplicate-reference-v1": "no_material_opportunity",
        "subresult-chunk-dedup-v1": "no_material_opportunity",
        "parallel-overlap-dedup-v1": "decline_framing_overhead",
    },
)
_expect(
    "exact_and_partial_overlap",
    ("cross_session_hash",),
    _uniform(OVERLAP_TECHNIQUES, "reject_cross_scope_access"),
)

_expect(
    "structured_json_and_tables",
    ("ordered_rows", "nulls", "numeric_vs_string"),
    _uniform(STRUCTURED_TECHNIQUES, "transform"),
)
_expect(
    "structured_json_and_tables",
    ("duplicate_keys", "heterogeneous_rows"),
    _uniform(STRUCTURED_TECHNIQUES, "decline_ineligible"),
)
_expect(
    "structured_json_and_tables",
    ("invalid_json", "changed_order_mutant"),
    _uniform(STRUCTURED_TECHNIQUES, "detect_corruption"),
)

_expect(
    "terminal_and_progress",
    ("sgr", "osc8"),
    {
        "ansi-osc-strip-v1": "transform",
        "line-ending-normalize-v1": "no_material_opportunity",
        "trailing-noise-trim-v1": "no_material_opportunity",
    },
)
_expect(
    "terminal_and_progress",
    ("carriage_return_spinner",),
    {
        "ansi-osc-strip-v1": "no_material_opportunity",
        "line-ending-normalize-v1": "no_material_opportunity",
        "trailing-noise-trim-v1": "transform",
    },
)
_expect(
    "terminal_and_progress",
    ("source_code_crlf", "signature_bytes"),
    _uniform(TERMINAL_TECHNIQUES, "decline_ineligible"),
)
_expect(
    "terminal_and_progress",
    ("unknown_control", "warning_output"),
    _uniform(TERMINAL_TECHNIQUES, "detect_corruption"),
)

_expect(
    "file_rereads_and_diffs",
    ("unchanged",),
    {
        "unchanged-file-identity-v1": "transform",
        "delta-reread-v1": "no_material_opportunity",
    },
)
_expect(
    "file_rereads_and_diffs",
    ("small_edit",),
    {
        "unchanged-file-identity-v1": "no_material_opportunity",
        "delta-reread-v1": "transform",
    },
)
_expect(
    "file_rereads_and_diffs",
    ("high_churn",),
    {
        "unchanged-file-identity-v1": "no_material_opportunity",
        "delta-reread-v1": "decline_framing_overhead",
    },
)
_expect(
    "file_rereads_and_diffs",
    ("binary",),
    _uniform(FILE_TECHNIQUES, "decline_ineligible"),
)
_expect(
    "file_rereads_and_diffs",
    ("stale_base", "edited_history_fork"),
    _uniform(FILE_TECHNIQUES, "fail_recovery"),
)

_expect(
    "commands_checks_and_logs",
    ("successful_long_check", "test_output", "build_output"),
    _uniform(COMMAND_TECHNIQUES, "transform"),
)
_expect(
    "commands_checks_and_logs",
    (
        "failed_check",
        "warning_on_success",
        "ambiguous_exit_status",
        "git_status",
        "git_diff",
    ),
    _uniform(COMMAND_TECHNIQUES, "decline_ineligible"),
)
_expect(
    "commands_checks_and_logs",
    ("unknown_version", "truncated", "oversized", "log_output"),
    _uniform(COMMAND_TECHNIQUES, "decline_ineligible"),
)

_expect(
    "cache_sensitive_prefixes",
    ("cold", "warm_same_prefix"),
    {
        "prefix-cache-shaping-v1": "transform",
        "provider-kv-reuse-v1": "decline_ineligible",
        "transport-gzip-v1": "no_material_opportunity",
    },
)
_expect(
    "cache_sensitive_prefixes",
    ("warm_changed_suffix",),
    {
        "prefix-cache-shaping-v1": "decline_cache_harm",
        "provider-kv-reuse-v1": "no_material_opportunity",
        "transport-gzip-v1": "no_material_opportunity",
    },
)
_expect(
    "cache_sensitive_prefixes",
    ("smaller_but_more_expensive",),
    _uniform(CACHE_TECHNIQUES, "decline_cache_harm"),
)
_expect(
    "cache_sensitive_prefixes",
    ("expired",),
    _uniform(CACHE_TECHNIQUES, "no_material_opportunity"),
)
_expect(
    "cache_sensitive_prefixes",
    ("missing_cache_fields",),
    {
        "prefix-cache-shaping-v1": "detect_corruption",
        "provider-kv-reuse-v1": "detect_corruption",
        "transport-gzip-v1": "no_material_opportunity",
    },
)

_expect(
    "cooperative_context",
    ("certified_context_epoch", "ordered_batch"),
    _uniform(COOPERATIVE_TECHNIQUES, "decline_ineligible"),
)
_expect(
    "cooperative_context",
    ("no_cloud_seam", "tool_discovery_needed", "no_op_poll"),
    {
        "context-paging-v1": "decline_ineligible",
        "schema-deferral-v1": "decline_ineligible",
        "turn-elimination-v1": "no_material_opportunity",
        "agent-memory-summary-v1": "decline_ineligible",
        "code-action-batching-v1": "decline_ineligible",
    },
)
_expect(
    "cooperative_context",
    ("failed_expansion", "changed_order_mutant"),
    {
        "context-paging-v1": "fail_recovery",
        "schema-deferral-v1": "detect_corruption",
        "turn-elimination-v1": "detect_corruption",
        "agent-memory-summary-v1": "fail_recovery",
        "code-action-batching-v1": "detect_corruption",
    },
)

_expect(
    "behavioral_and_lossy",
    ("own_text_only", "near_match_code_prompt", "visual_small_detail"),
    _uniform(BEHAVIORAL_TECHNIQUES, "decline_ineligible"),
)
_expect(
    "behavioral_and_lossy",
    ("changed_single_line", "required_detail_truncated"),
    _uniform(BEHAVIORAL_TECHNIQUES, "detect_corruption"),
)

_expect(
    "protocol_and_isolation",
    (
        "stream_order",
        "retry_identity",
        "manual_continuation",
        "previous_response_continuation",
        "conversation_continuation",
    ),
    _uniform(FROZEN_ENFORCEABLE_TECHNIQUES, "no_material_opportunity"),
)
_expect(
    "protocol_and_isolation",
    ("cancelled_stream", "401", "429", "500"),
    _uniform(FROZEN_ENFORCEABLE_TECHNIQUES, "decline_ineligible"),
)
_expect(
    "protocol_and_isolation",
    ("cross_tenant", "cross_principal", "cross_session", "unauthorized_artifact"),
    _uniform(FROZEN_ENFORCEABLE_TECHNIQUES, "reject_cross_scope_access"),
)


def _ordinary_dispositions(enforceable_disposition: str) -> dict[str, str]:
    return _uniform(FROZEN_ENFORCEABLE_TECHNIQUES, enforceable_disposition) | {
        "routing-context-profile-v1": "decline_ineligible",
        "learned-soft-compression-v1": "decline_ineligible",
        "speculative-decoding-v1": "no_material_opportunity",
    }


_expect(
    "ordinary_no_op_traffic",
    ("no_candidate",),
    _ordinary_dispositions("no_material_opportunity"),
)
_expect(
    "ordinary_no_op_traffic",
    ("candidate_not_smaller",),
    _ordinary_dispositions("decline_framing_overhead"),
)
_expect(
    "ordinary_no_op_traffic",
    ("candidate_cache_harm",),
    _ordinary_dispositions("decline_cache_harm"),
)
_expect(
    "ordinary_no_op_traffic",
    ("unknown_dialect", "zero_retention"),
    _ordinary_dispositions("decline_ineligible"),
)

if len(EXPECTED_DISPOSITIONS_BY_CASE) != 79:
    raise AssertionError(
        "independent disposition oracle must contain 79 family/case rows"
    )


def apply_unified_diff(base: str, patch: str) -> str:
    """Apply the deterministic single-file unified diff used by this corpus."""
    source = base.splitlines(keepends=True)
    diff_lines = patch.splitlines(keepends=True)
    output: list[str] = []
    source_index = 0
    in_hunk = False
    for line in diff_lines:
        if line.startswith(("--- ", "+++ ")):
            continue
        if line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match is None:
                raise AssertionError(f"invalid unified diff hunk: {line!r}")
            old_start = int(match.group(1)) - 1
            output.extend(source[source_index:old_start])
            source_index = old_start
            in_hunk = True
            continue
        if not in_hunk:
            continue
        marker, content = line[:1], line[1:]
        if marker == " ":
            if source[source_index] != content:
                raise AssertionError("unified diff context does not match base")
            output.append(content)
            source_index += 1
        elif marker == "-":
            if source[source_index] != content:
                raise AssertionError("unified diff deletion does not match base")
            source_index += 1
        elif marker == "+":
            output.append(content)
        elif line.startswith("\\ No newline at end of file"):
            continue
        else:
            raise AssertionError(f"unsupported unified diff line: {line!r}")
    output.extend(source[source_index:])
    return "".join(output)


class Qa56CompandTechniqueCorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(CONTRACT / "corpus-manifest.json")
        cls.catalog = load_json(CONTRACT / "technique-catalog.json")
        cls.enforceable_technique_ids = {
            item["id"]
            for item in cls.catalog["techniques"]
            if item["cloud_gateway_enforceable"]
        }
        cls.index = load_json(CORPUS / "index.json")
        cls.holdout = load_json(CORPUS / "hidden-holdout-plan.json")
        cls.fixture_paths = [
            CORPUS / item["path"]
            for partition in ("development", "golden")
            for item in cls.index["partitions"][partition]["fixtures"]
        ]
        cls.fixtures = [load_json(path) for path in cls.fixture_paths]
        cls.case_records = [
            (fixture, record)
            for fixture in cls.fixtures
            for record in fixture["case_records"]
        ]

    def test_generator_reports_no_drift(self) -> None:
        result = subprocess.run(
            [sys.executable, str(GENERATOR)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified", result.stdout)

    def test_checksums_cover_every_generated_corpus_file(self) -> None:
        declared = {}
        for line in (CORPUS / "CHECKSUMS").read_text(encoding="utf-8").splitlines():
            digest, relative = line.split("  ", 1)
            self.assertNotIn(relative, declared)
            declared[relative] = digest
        actual = {
            path.relative_to(CORPUS).as_posix(): sha256_bytes(path.read_bytes())
            for path in CORPUS.rglob("*")
            if path.is_file() and path.name != "CHECKSUMS"
        }
        self.assertEqual(declared, actual)

    def test_manifest_and_partition_roots_are_recomputable(self) -> None:
        materialization = self.manifest["materialization"]
        self.assertFalse(materialization["confirmatory_ready"])
        self.assertEqual(
            materialization["corpus_root_sha256"],
            self.index["corpus_root_sha256"],
        )
        self.assertEqual(materialization["visible_fixture_count"], 33)
        self.assertEqual(materialization["visible_case_record_count"], 103)
        self.assertEqual(materialization["hidden_holdout_fixture_count"], 0)
        self.assertEqual(materialization["hidden_holdout_reservation_count"], 11)

        for partition_name in ("development", "golden"):
            partition = self.index["partitions"][partition_name]
            expected_root = sha256_json(
                [
                    {"path": item["path"], "sha256": item["sha256"]}
                    for item in partition["fixtures"]
                ]
            )
            self.assertEqual(partition["root_sha256"], expected_root)

        manifest_without_hash = copy.deepcopy(self.manifest)
        manifest_without_hash["materialization"]["manifest_sha256"] = None
        self.assertEqual(
            materialization["manifest_sha256"], sha256_json(manifest_without_hash)
        )

        expected_corpus_root = sha256_json(
            {
                "version": self.index["version"],
                "development_root_sha256": self.index["partitions"]["development"][
                    "root_sha256"
                ],
                "golden_root_sha256": self.index["partitions"]["golden"]["root_sha256"],
                "hidden_holdout_plan_sha256": self.index["partitions"][
                    "hidden_holdout"
                ]["plan_sha256"],
                "baseline_outcome_sha256": self.index["baseline_task_outcome_sha256"],
            }
        )
        self.assertEqual(self.index["corpus_root_sha256"], expected_corpus_root)

    def test_visible_fixtures_obey_contract_and_pin_baseline_bytes(self) -> None:
        required = set(self.manifest["fixture_contract"]["required_fields"])
        allowed = set(self.manifest["fixture_contract"]["allowed_dispositions"])
        seen_ids = set()
        seen_dispositions = set()
        cases_by_family: dict[str, set[str]] = {}
        technique_ids = set()
        outcomes = []
        indexed = {
            item["path"]: item
            for partition in ("development", "golden")
            for item in self.index["partitions"][partition]["fixtures"]
        }

        for path, fixture in zip(self.fixture_paths, self.fixtures, strict=True):
            with self.subTest(fixture=fixture["fixture_id"]):
                relative = path.relative_to(CORPUS).as_posix()
                self.assertEqual(
                    indexed[relative]["sha256"], sha256_bytes(path.read_bytes())
                )
                self.assertEqual(fixture["schema"], "compand.ces1.fixture.v2")
                self.assertEqual(required - set(fixture), set())
                self.assertNotIn(fixture["fixture_id"], seen_ids)
                seen_ids.add(fixture["fixture_id"])
                self.assertEqual(fixture["input_sha256"], sha256_json(fixture["input"]))
                self.assertEqual(
                    fixture["baseline_provider_view_sha256"],
                    sha256_json(fixture["baseline_provider_view"]),
                )
                self.assertEqual(
                    fixture["case_record_count"], len(fixture["case_records"])
                )
                self.assertIn(
                    "baseline_provider_view_is_byte_unchanged_B0",
                    fixture["required_invariants"],
                )
                self.assertEqual(
                    fixture["license"], {"reviewed": True, "spdx": "CC0-1.0"}
                )
                self.assertEqual(
                    fixture["generator_or_source"]["kind"], "sanitized_synthetic"
                )
                self.assertFalse(fixture["generator_or_source"]["raw_customer_content"])
                dispositions = {
                    disposition
                    for record in fixture["case_records"]
                    for disposition in record[
                        "expected_disposition_by_technique"
                    ].values()
                }
                self.assertTrue(dispositions)
                self.assertEqual(dispositions - allowed, set())
                seen_dispositions.update(dispositions)
                for record in fixture["case_records"]:
                    self.assertEqual(record["schema"], "compand.ces1.case_record.v1")
                    self.assertEqual(
                        record["input_sha256"], sha256_json(record["input"])
                    )
                    self.assertEqual(
                        record["baseline_provider_view_sha256"],
                        sha256_json(record["baseline_provider_view"]),
                    )
                    self.assertEqual(
                        record["input"]["provider_request"],
                        record["baseline_provider_view"],
                    )
                    self.assertEqual(
                        record["input"]["scenario"]["case_id"], record["case_id"]
                    )
                    technique_ids.update(
                        record["expected_disposition_by_technique"].keys()
                    )
                    cases_by_family.setdefault(fixture["fixture_family"], set()).add(
                        record["case_id"]
                    )
                    outcomes.append(
                        {
                            "record_id": record["record_id"],
                            "expected_outcome": record["expected_outcome"],
                        }
                    )
                serialized = json.dumps(fixture, ensure_ascii=False)
                for forbidden in ("/Users/", "gho_", "sk-proj-", "Bearer "):
                    self.assertNotIn(forbidden, serialized)
                self.assertEqual(
                    path.relative_to(CORPUS).parts[1], fixture["partition"]
                )

        self.assertEqual(seen_dispositions, allowed)
        self.assertEqual(
            technique_ids,
            {item["id"] for item in self.catalog["techniques"]},
        )
        for family in self.manifest["fixture_families"]:
            self.assertEqual(
                set(family["required_cases"]) - cases_by_family[family["id"]], set()
            )
        outcomes.sort(key=lambda item: item["record_id"])
        self.assertEqual(
            self.index["baseline_task_outcome_sha256"], sha256_json(outcomes)
        )

    def test_partition_was_frozen_before_plugin_tuning(self) -> None:
        development = self.index["partitions"]["development"]["fixture_count"]
        golden = self.index["partitions"]["golden"]["fixture_count"]
        holdout = self.index["partitions"]["hidden_holdout"]["reservation_count"]
        self.assertEqual((development, golden, holdout), (22, 11, 11))
        total_slots = development + golden + holdout
        self.assertEqual(development / total_slots, 0.50)
        self.assertEqual(golden / total_slots, 0.25)
        self.assertEqual(holdout / total_slots, 0.25)
        self.assertEqual(
            self.index["partitions"]["development"]["case_record_count"], 53
        )
        self.assertEqual(self.index["partitions"]["golden"]["case_record_count"], 50)

    def test_golden_partition_has_positive_and_negative_transform_oracles(self) -> None:
        golden = [
            record
            for fixture, record in self.case_records
            if fixture["partition"] == "golden"
        ]
        self.assertEqual(
            {record["oracle_class"] for record in golden}, {"positive", "negative"}
        )
        self.assertTrue(
            any(
                record["oracle_class"] == "positive"
                and "transform" in record["expected_disposition_by_technique"].values()
                for record in golden
            )
        )
        self.assertTrue(
            any(
                record["oracle_class"] == "negative"
                and "transform"
                not in record["expected_disposition_by_technique"].values()
                for record in golden
            )
        )

    def test_every_case_has_an_exhaustive_per_technique_oracle(self) -> None:
        techniques_by_family = {
            "repeated_lines": {"line-rle-v1"},
            "exact_and_partial_overlap": {
                "exact-duplicate-reference-v1",
                "subresult-chunk-dedup-v1",
                "parallel-overlap-dedup-v1",
            },
            "structured_json_and_tables": {
                "json-minify-v1",
                "structured-data-codec-v1",
            },
            "terminal_and_progress": {
                "ansi-osc-strip-v1",
                "line-ending-normalize-v1",
                "trailing-noise-trim-v1",
            },
            "file_rereads_and_diffs": {
                "unchanged-file-identity-v1",
                "delta-reread-v1",
            },
            "commands_checks_and_logs": {
                "command-aware-projection-v1",
                "successful-check-projection-v1",
            },
            "cache_sensitive_prefixes": {
                "prefix-cache-shaping-v1",
                "provider-kv-reuse-v1",
                "transport-gzip-v1",
            },
            "cooperative_context": {
                "context-paging-v1",
                "schema-deferral-v1",
                "turn-elimination-v1",
                "agent-memory-summary-v1",
                "code-action-batching-v1",
            },
            "behavioral_and_lossy": {
                "semantic-cache-v1",
                "injected-efficiency-instructions-v1",
                "injected-text-hard-compression-v1",
                "output-shaping-v1",
                "vision-budget-v1",
                "lean-prompt-v1",
            },
            "protocol_and_isolation": self.enforceable_technique_ids,
            "ordinary_no_op_traffic": self.enforceable_technique_ids
            | {
                "routing-context-profile-v1",
                "learned-soft-compression-v1",
                "speculative-decoding-v1",
            },
        }
        for fixture, record in self.case_records:
            with self.subTest(
                family=fixture["fixture_family"], case_id=record["case_id"]
            ):
                dispositions = record["expected_disposition_by_technique"]
                self.assertEqual(
                    set(dispositions), techniques_by_family[fixture["fixture_family"]]
                )
                self.assertEqual(
                    record["eligible_techniques"],
                    sorted(
                        technique_id
                        for technique_id, disposition in dispositions.items()
                        if technique_id in self.enforceable_technique_ids
                        and disposition == "transform"
                    ),
                )

    def test_all_103_records_match_independent_frozen_disposition_matrix(self) -> None:
        self.assertEqual(len(self.case_records), 103)
        self.assertEqual(
            set(FROZEN_ENFORCEABLE_TECHNIQUES), self.enforceable_technique_ids
        )
        observed_canonical: dict[tuple[str, str], dict[str, str]] = {}
        for fixture, record in self.case_records:
            key = (fixture["fixture_family"], record["case_id"])
            with self.subTest(partition=fixture["partition"], key=key):
                self.assertIn(key, EXPECTED_DISPOSITIONS_BY_CASE)
                expected = EXPECTED_DISPOSITIONS_BY_CASE[key]
                self.assertEqual(record["expected_disposition_by_technique"], expected)
                self.assertEqual(
                    record["eligible_techniques"],
                    sorted(
                        technique_id
                        for technique_id, disposition in expected.items()
                        if technique_id in FROZEN_ENFORCEABLE_TECHNIQUES
                        and disposition == "transform"
                    ),
                )
                if key in observed_canonical:
                    self.assertEqual(observed_canonical[key], expected)
                else:
                    observed_canonical[key] = expected
        self.assertEqual(set(observed_canonical), set(EXPECTED_DISPOSITIONS_BY_CASE))

    def _assert_transform_eligible(self, technique_id: str, scenario: dict) -> None:
        if technique_id == "line-rle-v1":
            source = scenario["output"]
            candidate = scenario["rle_candidate_utf8"]
            decoded = json.loads(candidate)
            reconstructed = decoded["line"] * decoded["count"]
            self.assertEqual(reconstructed, source)
            self.assertEqual(
                sha256_bytes(reconstructed.encode()), scenario["source_sha256"]
            )
            self.assertEqual(scenario["actual_repeat_count"], decoded["count"])
            self.assertGreater(decoded["count"], 1)
            self.assertEqual(scenario["source_byte_count"], len(source.encode()))
            self.assertEqual(
                scenario["rle_candidate_byte_count"], len(candidate.encode())
            )
            self.assertLess(
                scenario["rle_candidate_byte_count"], scenario["source_byte_count"]
            )
            return

        if technique_id == "exact-duplicate-reference-v1":
            source = scenario["provider_visible_source"]
            candidate = scenario["new_results"][scenario["target_candidate_index"]]
            reference = scenario["exact_duplicate_candidate_utf8"]
            decoded = json.loads(reference)
            self.assertEqual(scenario["source_state"], "visible")
            self.assertEqual(scenario["source_scope"], scenario["requester_scope"])
            self.assertEqual(source, candidate)
            self.assertEqual(decoded["version"], technique_id)
            self.assertEqual(decoded["scope"], scenario["requester_scope"])
            self.assertEqual(decoded["sha256"], sha256_bytes(source.encode("utf-8")))
            reconstructed = {decoded["sha256"]: source}[decoded["sha256"]]
            self.assertEqual(reconstructed, candidate)
            self.assertEqual(sha256_bytes(reconstructed.encode()), decoded["sha256"])
            self.assertEqual(
                scenario["exact_duplicate_candidate_byte_count"],
                len(reference.encode("utf-8")),
            )
            self.assertLess(
                scenario["exact_duplicate_candidate_byte_count"],
                scenario["target_candidate_byte_count"],
            )
            return

        if technique_id == "subresult-chunk-dedup-v1":
            source = scenario["provider_visible_source"].encode("utf-8")
            target = scenario["new_results"][scenario["target_candidate_index"]].encode(
                "utf-8"
            )
            expected_source_chunks = independent_fastcdc_chunks(source)
            expected_target_chunks = independent_fastcdc_chunks(target)
            self.assertEqual(scenario["fastcdc_source_chunks"], expected_source_chunks)
            self.assertEqual(scenario["fastcdc_target_chunks"], expected_target_chunks)
            decoded = json.loads(scenario["subresult_chunk_candidate_utf8"])
            self.assertEqual(decoded["version"], technique_id)
            self.assertEqual(decoded["scope"], scenario["requester_scope"])
            reconstructed = bytearray()
            referenced = 0
            for expected_chunk, encoded in zip(
                expected_target_chunks, decoded["chunks"], strict=True
            ):
                if encoded["kind"] == "reference":
                    source_chunk = expected_source_chunks[
                        encoded["source_chunk_index"]
                    ]
                    self.assertEqual(source_chunk["sha256"], encoded["sha256"])
                    chunk_bytes = source[
                        source_chunk["offset"] : source_chunk["offset"]
                        + source_chunk["byte_count"]
                    ]
                    referenced += len(chunk_bytes)
                else:
                    self.assertEqual(encoded["kind"], "literal_utf8")
                    chunk_bytes = encoded["utf8"].encode("utf-8")
                self.assertEqual(sha256_bytes(chunk_bytes), expected_chunk["sha256"])
                reconstructed.extend(chunk_bytes)
            self.assertEqual(bytes(reconstructed), target)
            self.assertEqual(sha256_bytes(bytes(reconstructed)), sha256_bytes(target))
            self.assertEqual(scenario["content_defined_overlap_byte_count"], referenced)
            self.assertEqual(
                scenario["content_defined_overlap_ratio"], referenced / len(target)
            )
            self.assertGreaterEqual(scenario["content_defined_overlap_ratio"], 0.70)
            self.assertLess(
                scenario["subresult_chunk_candidate_byte_count"], len(target)
            )
            return

        if technique_id == "parallel-overlap-dedup-v1":
            source = scenario["provider_visible_source"]
            decoded = json.loads(scenario["parallel_overlap_candidate_utf8"])
            self.assertEqual(decoded["version"], technique_id)
            self.assertEqual(decoded["scope"], scenario["requester_scope"])
            self.assertEqual(
                [sibling["index"] for sibling in decoded["siblings"]],
                list(range(len(scenario["new_results"]))),
            )
            reconstructed_siblings = []
            for sibling in decoded["siblings"]:
                reconstructed = []
                for piece in sibling["pieces"]:
                    if piece["kind"] == "provider_visible_reference":
                        self.assertEqual(
                            piece["sha256"], sha256_bytes(source.encode("utf-8"))
                        )
                        reconstructed.append(source)
                    else:
                        self.assertEqual(piece["kind"], "literal_utf8")
                        reconstructed.append(piece["utf8"])
                reconstructed_siblings.append("".join(reconstructed))
            self.assertEqual(reconstructed_siblings, scenario["new_results"])
            self.assertEqual(
                [sha256_bytes(item.encode()) for item in reconstructed_siblings],
                [sha256_bytes(item.encode()) for item in scenario["new_results"]],
            )
            self.assertEqual(
                scenario["parallel_source_byte_count"],
                sum(len(item.encode()) for item in scenario["new_results"]),
            )
            self.assertLess(
                scenario["parallel_overlap_candidate_byte_count"],
                scenario["parallel_source_byte_count"],
            )
            return

        if technique_id in {"json-minify-v1", "structured-data-codec-v1"}:
            source = scenario["source_json_utf8"]
            parsed_source = json.loads(source)
            self.assertEqual(parsed_source, scenario["typed_json"])
            self.assertEqual(
                scenario["source_json_byte_count"], len(source.encode("utf-8"))
            )
            if technique_id == "json-minify-v1":
                candidate = scenario["json_minified_candidate_utf8"]
                self.assertEqual(json.loads(candidate), parsed_source)
                self.assertEqual(
                    candidate,
                    json.dumps(
                        parsed_source,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                candidate_count = scenario["json_minified_candidate_byte_count"]
            else:
                candidate = scenario["structured_codec_candidate_utf8"]
                codec = json.loads(candidate)
                reconstructed = [
                    dict(zip(codec["columns"], row, strict=True))
                    for row in codec["rows"]
                ]
                self.assertEqual(reconstructed, parsed_source)
                self.assertEqual(codec["version"], "ordered-table-v1")
                candidate_count = scenario["structured_codec_candidate_byte_count"]
            self.assertEqual(candidate_count, len(candidate.encode("utf-8")))
            self.assertLess(candidate_count, scenario["source_json_byte_count"])
            return

        if technique_id in {"ansi-osc-strip-v1", "trailing-noise-trim-v1"}:
            source = scenario["terminal_bytes_utf8"]
            candidate = scenario["transform_candidates_utf8"][technique_id]
            self.assertIsNotNone(candidate)
            self.assertEqual(scenario["source_byte_count"], len(source.encode("utf-8")))
            self.assertEqual(
                scenario["transform_candidate_byte_counts"][technique_id],
                len(candidate.encode("utf-8")),
            )
            self.assertLess(len(candidate.encode()), len(source.encode()))
            if technique_id == "ansi-osc-strip-v1":
                self.assertNotIn("\x1b", candidate)
                if scenario["case_id"] == "sgr":
                    self.assertEqual(candidate, "PASS\n")
                else:
                    self.assertIn("fixture link", candidate)
                    self.assertIn("https://fixture.invalid", candidate)
            else:
                self.assertEqual(candidate, source.split("\r")[-1])
                self.assertNotIn("warning", source.lower())
            return

        if technique_id == "delta-reread-v1":
            patch = scenario["unified_diff_utf8"]
            expected_patch = "".join(
                difflib.unified_diff(
                    scenario["base"].splitlines(keepends=True),
                    scenario["current"].splitlines(keepends=True),
                    fromfile="a/synthetic/module.py",
                    tofile="b/synthetic/module.py",
                )
            )
            self.assertEqual(patch, expected_patch)
            self.assertTrue(scenario["trusted_file_identity"])
            self.assertFalse(scenario["binary"])
            self.assertEqual(
                apply_unified_diff(scenario["base"], patch), scenario["current"]
            )
            self.assertEqual(
                sha256_bytes(scenario["current"].encode()), scenario["current_sha256"]
            )
            self.assertEqual(scenario["patch_byte_count"], len(patch.encode()))
            self.assertEqual(
                scenario["current_byte_count"], len(scenario["current"].encode())
            )
            self.assertAlmostEqual(
                scenario["patch_to_current_ratio"],
                scenario["patch_byte_count"] / scenario["current_byte_count"],
            )
            self.assertLess(scenario["patch_to_current_ratio"], 0.60)
            return

        if technique_id == "unchanged-file-identity-v1":
            receipt = scenario["typed_reread_receipt"]
            reference_utf8 = scenario["unchanged_reference_candidate_utf8"]
            reference = json.loads(reference_utf8)
            self.assertTrue(scenario["trusted_file_identity"])
            self.assertFalse(scenario["binary"])
            self.assertEqual(scenario["base"], scenario["current"])
            self.assertEqual(scenario["base_sha256"], scenario["current_sha256"])
            self.assertEqual(receipt["schema"], "compand.file_reread.v1")
            self.assertTrue(receipt["trusted_adapter"])
            self.assertEqual(receipt["path"], scenario["path"])
            self.assertEqual(receipt["repository_state"], scenario["repository_state"])
            self.assertEqual(receipt["encoding"], "utf-8")
            self.assertFalse(receipt["truncated"])
            self.assertFalse(receipt["signed"])
            self.assertEqual(receipt["byte_count"], len(scenario["current"].encode()))
            self.assertEqual(receipt["output_sha256"], scenario["current_sha256"])
            self.assertEqual(
                scenario["provider_visible_source_utf8"], scenario["current"]
            )
            self.assertEqual(
                scenario["provider_visible_source_sha256"],
                sha256_bytes(scenario["provider_visible_source_utf8"].encode()),
            )
            self.assertEqual(
                scenario["provider_visible_source_scope"], scenario["requester_scope"]
            )
            self.assertEqual(reference["version"], technique_id)
            self.assertEqual(reference["path"], scenario["path"])
            self.assertEqual(reference["scope"], scenario["requester_scope"])
            self.assertEqual(reference["artifact_sha256"], scenario["current_sha256"])
            reconstructed = {
                reference["artifact_sha256"]: scenario["artifact_expansion_utf8"]
            }[reference["artifact_sha256"]]
            self.assertEqual(reconstructed, scenario["current"])
            self.assertEqual(
                sha256_bytes(reconstructed.encode()), scenario["current_sha256"]
            )
            self.assertEqual(
                scenario["unchanged_reference_candidate_byte_count"],
                len(reference_utf8.encode()),
            )
            self.assertLess(
                scenario["unchanged_reference_candidate_byte_count"],
                scenario["current_byte_count"],
            )
            return

        if technique_id in {
            "command-aware-projection-v1",
            "successful-check-projection-v1",
        }:
            source = scenario["output_utf8"]
            candidate = scenario["projection_candidate_utf8"]
            projection = json.loads(candidate)
            receipt = scenario["typed_command_result_receipt"]
            self.assertEqual(scenario["projection_version"], "commands-v1")
            self.assertEqual(receipt["schema"], "compand.command_result.v1")
            self.assertEqual(
                receipt["call_id"], f"call-{scenario['synthetic_record_id']}"
            )
            self.assertEqual(receipt["source_kind"], "command_result")
            self.assertTrue(receipt["trusted_adapter"])
            self.assertIs(type(scenario["exit_status"]), int)
            self.assertEqual(scenario["exit_status"], 0)
            self.assertIs(type(receipt["exit_status"]), int)
            self.assertEqual(receipt["exit_status"], 0)
            self.assertEqual(receipt["content_type"], "text/plain")
            self.assertEqual(receipt["encoding"], "utf-8")
            self.assertFalse(receipt["truncated"])
            self.assertFalse(receipt["signed"])
            self.assertTrue(receipt["new_suffix"])
            self.assertTrue(receipt["expected_eligible"])
            self.assertFalse(scenario["warning_present"])
            self.assertFalse(scenario["truncated"])
            self.assertGreaterEqual(scenario["output_line_count"], 80)
            self.assertEqual(scenario["output_sha256"], sha256_bytes(source.encode()))
            self.assertEqual(receipt["output_utf8"], source)
            self.assertEqual(receipt["byte_count"], len(source.encode()))
            self.assertEqual(receipt["output_sha256"], sha256_bytes(source.encode()))
            self.assertEqual(projection["artifact_sha256"], scenario["output_sha256"])
            self.assertEqual(projection["command"], scenario["command"])
            self.assertEqual(projection["exit_status"], 0)
            self.assertEqual(projection["tail"], scenario["lines"][-2:])
            reconstructed = {
                projection["artifact_sha256"]: scenario["artifact_expansion_utf8"]
            }[projection["artifact_sha256"]]
            self.assertEqual(reconstructed, source)
            self.assertEqual(
                sha256_bytes(reconstructed.encode()), projection["artifact_sha256"]
            )
            self.assertEqual(scenario["output_byte_count"], len(source.encode()))
            self.assertEqual(
                scenario["projection_candidate_byte_count"], len(candidate.encode())
            )
            self.assertLess(
                scenario["projection_candidate_byte_count"],
                scenario["output_byte_count"],
            )
            return

        if technique_id == "prefix-cache-shaping-v1":
            self.assertEqual(
                scenario["provider_visible_prefix_utf8"],
                scenario["replayed_prefix_utf8"],
            )
            self.assertTrue(scenario["cache_fields_present"])
            self.assertTrue(scenario["candidate_smaller"])
            self.assertLess(
                scenario["projected_candidate_cost_microusd"],
                scenario["projected_original_cost_microusd"],
            )
            self.assertEqual(
                scenario["provider_visible_prefix_byte_count"],
                len(scenario["provider_visible_prefix_utf8"].encode()),
            )
            self.assertEqual(
                scenario["candidate_suffix_byte_count"],
                len(scenario["candidate_suffix_utf8"].encode()),
            )
            return

        self.fail(f"no independent transform eligibility assertion for {technique_id}")

    def test_every_transform_disposition_has_computed_eligibility(self) -> None:
        checked = 0
        for fixture, record in self.case_records:
            for technique_id, disposition in record[
                "expected_disposition_by_technique"
            ].items():
                if disposition != "transform":
                    continue
                with self.subTest(
                    partition=fixture["partition"],
                    family=fixture["fixture_family"],
                    case_id=record["case_id"],
                    technique_id=technique_id,
                ):
                    self._assert_transform_eligible(
                        technique_id, record["input"]["scenario"]
                    )
                    checked += 1
        self.assertGreater(checked, 0)

    def test_required_edge_cases_have_independent_semantic_inputs_and_oracles(
        self,
    ) -> None:
        records: dict[tuple[str, str, str], dict] = {}
        for fixture, record in self.case_records:
            key = (fixture["partition"], fixture["fixture_family"], record["case_id"])
            self.assertNotIn(key, records)
            records[key] = record

        def development(family: str, case_id: str) -> dict:
            return records[("development", family, case_id)]

        def any_partition(family: str, case_id: str) -> dict:
            return next(
                record
                for (partition, fixture_family, candidate), record in records.items()
                if fixture_family == family and candidate == case_id
            )

        for family in self.manifest["fixture_families"]:
            for case_id in family["required_cases"]:
                candidates = [
                    record
                    for (
                        partition,
                        fixture_family,
                        candidate,
                    ), record in records.items()
                    if fixture_family == family["id"] and candidate == case_id
                ]
                self.assertTrue(
                    candidates, f"missing concrete record for {family['id']}:{case_id}"
                )
                self.assertTrue(
                    all(
                        record["input"]["scenario"]["case_id"] == case_id
                        for record in candidates
                    )
                )

        missing = development("exact_and_partial_overlap", "missing_source")
        evicted = development("exact_and_partial_overlap", "evicted_source")
        below_threshold = development(
            "exact_and_partial_overlap", "below_70_percent_overlap"
        )
        cross_session = next(
            record
            for fixture, record in self.case_records
            if fixture["partition"] == "golden"
            and fixture["fixture_family"] == "exact_and_partial_overlap"
            and record["case_id"] == "cross_session_hash"
        )
        self.assertEqual(missing["input"]["scenario"]["source_state"], "missing")
        self.assertFalse(missing["input"]["scenario"]["source_artifact_recoverable"])
        self.assertEqual(evicted["input"]["scenario"]["source_state"], "evicted")
        self.assertNotEqual(
            cross_session["input"]["scenario"]["source_scope"],
            cross_session["input"]["scenario"]["requester_scope"],
        )
        self.assertEqual(
            set(cross_session["expected_disposition_by_technique"].values()),
            {"reject_cross_scope_access"},
        )
        below_scenario = below_threshold["input"]["scenario"]
        source = below_scenario["provider_visible_source"]
        target = below_scenario["new_results"][below_scenario["target_candidate_index"]]
        self.assertNotIn(source, below_scenario["new_results"])
        self.assertEqual(
            below_scenario["target_candidate_sha256"],
            sha256_bytes(target.encode("utf-8")),
        )
        self.assertEqual(
            below_scenario["overlap_ratio"],
            difflib.SequenceMatcher(
                None,
                source.encode("utf-8"),
                target.encode("utf-8"),
                autojunk=False,
            ).ratio(),
        )
        self.assertLess(below_scenario["overlap_ratio"], 0.70)
        self.assertEqual(
            below_threshold["expected_disposition_by_technique"],
            {
                "exact-duplicate-reference-v1": "no_material_opportunity",
                "parallel-overlap-dedup-v1": "decline_framing_overhead",
                "subresult-chunk-dedup-v1": "no_material_opportunity",
            },
        )

        terminal_expectations = {
            "sgr": {
                "ansi-osc-strip-v1": "transform",
                "line-ending-normalize-v1": "no_material_opportunity",
                "trailing-noise-trim-v1": "no_material_opportunity",
            },
            "osc8": {
                "ansi-osc-strip-v1": "transform",
                "line-ending-normalize-v1": "no_material_opportunity",
                "trailing-noise-trim-v1": "no_material_opportunity",
            },
            "carriage_return_spinner": {
                "ansi-osc-strip-v1": "no_material_opportunity",
                "line-ending-normalize-v1": "no_material_opportunity",
                "trailing-noise-trim-v1": "transform",
            },
        }
        for case_id, expected in terminal_expectations.items():
            self.assertEqual(
                development("terminal_and_progress", case_id)[
                    "expected_disposition_by_technique"
                ],
                expected,
            )

        small_edit = development("file_rereads_and_diffs", "small_edit")
        self.assertNotEqual(
            small_edit["input"]["scenario"]["base_sha256"],
            small_edit["input"]["scenario"]["current_sha256"],
        )
        self.assertEqual(
            small_edit["expected_disposition_by_technique"],
            {
                "delta-reread-v1": "transform",
                "unchanged-file-identity-v1": "no_material_opportunity",
            },
        )

        for case_id in ("successful_long_check", "test_output", "build_output"):
            record = any_partition("commands_checks_and_logs", case_id)
            scenario = record["input"]["scenario"]
            provider_item = record["input"]["provider_request"]["body"]["input"][0]
            receipt = scenario["typed_command_result_receipt"]
            self.assertEqual(provider_item["output"], scenario["output_utf8"])
            self.assertEqual(provider_item["call_id"], receipt["call_id"])
            self.assertEqual(receipt["byte_count"], len(provider_item["output"].encode()))
            self.assertEqual(
                receipt["output_sha256"], sha256_bytes(provider_item["output"].encode())
            )
            self.assertEqual(record["baseline_provider_view"], record["input"]["provider_request"])
            self.assertEqual(
                set(record["expected_disposition_by_technique"].values()),
                {"transform"},
            )

        for case_id in ("unknown_version", "truncated", "oversized", "log_output"):
            record = any_partition("commands_checks_and_logs", case_id)
            scenario = record["input"]["scenario"]
            provider_item = record["input"]["provider_request"]["body"]["input"][0]
            receipt = scenario["typed_command_result_receipt"]
            provider_bytes = provider_item["output"].encode("utf-8")
            self.assertEqual(provider_item["call_id"], receipt["call_id"])
            self.assertEqual(provider_item["output"], scenario["output_utf8"])
            self.assertEqual(receipt["byte_count"], len(provider_bytes))
            self.assertEqual(receipt["output_sha256"], sha256_bytes(provider_bytes))
            self.assertEqual(scenario["output_byte_count"], len(provider_bytes))
            self.assertEqual(scenario["output_sha256"], sha256_bytes(provider_bytes))
            self.assertTrue(scenario["passthrough_required"])
            self.assertFalse(receipt["expected_eligible"])
            self.assertEqual(record["baseline_provider_view"], record["input"]["provider_request"])
            self.assertEqual(
                set(record["expected_disposition_by_technique"].values()),
                {"decline_ineligible"},
            )
        unknown = any_partition("commands_checks_and_logs", "unknown_version")[
            "input"
        ]["scenario"]
        self.assertNotEqual(unknown["projection_version"], "commands-v1")
        self.assertIsNotNone(unknown["projection_candidate_utf8"])
        truncated = any_partition("commands_checks_and_logs", "truncated")["input"][
            "scenario"
        ]
        self.assertTrue(truncated["typed_command_result_receipt"]["truncated"])
        oversized = any_partition("commands_checks_and_logs", "oversized")["input"][
            "scenario"
        ]
        self.assertGreater(
            oversized["output_byte_count"], oversized["maximum_eligible_output_bytes"]
        )
        self.assertEqual(oversized["maximum_eligible_output_bytes"], 1_048_576)
        log_output = any_partition("commands_checks_and_logs", "log_output")["input"][
            "scenario"
        ]
        self.assertEqual(log_output["output_kind"], "log")
        self.assertEqual(
            log_output["typed_command_result_receipt"]["source_kind"], "log_result"
        )

        cache_harm = development("ordinary_no_op_traffic", "candidate_cache_harm")
        cache_harm_scenario = cache_harm["input"]["scenario"]
        self.assertTrue(cache_harm_scenario["candidate_smaller"])
        self.assertTrue(cache_harm_scenario["candidate_cache_harm"])
        cache_harm_dispositions = cache_harm["expected_disposition_by_technique"]
        self.assertEqual(
            {
                cache_harm_dispositions[technique_id]
                for technique_id in self.enforceable_technique_ids
            },
            {"decline_cache_harm"},
        )
        self.assertEqual(
            {
                technique_id: cache_harm_dispositions[technique_id]
                for technique_id in (
                    "routing-context-profile-v1",
                    "learned-soft-compression-v1",
                    "speculative-decoding-v1",
                )
            },
            {
                "routing-context-profile-v1": "decline_ineligible",
                "learned-soft-compression-v1": "decline_ineligible",
                "speculative-decoding-v1": "no_material_opportunity",
            },
        )

        cache_expectations = {
            "cold": ("cold", 0),
            "warm_same_prefix": ("warm_identical_prefix", 128),
            "warm_changed_suffix": ("warm_changed_suffix", 64),
            "expired": ("expired", 0),
        }
        for case_id, (condition, cached_tokens) in cache_expectations.items():
            scenario = any_partition("cache_sensitive_prefixes", case_id)["input"][
                "scenario"
            ]
            self.assertEqual(scenario["cache_condition"], condition)
            self.assertEqual(scenario["provider_cached_tokens"], cached_tokens)
        expensive = development(
            "cache_sensitive_prefixes", "smaller_but_more_expensive"
        )
        self.assertGreater(
            expensive["input"]["scenario"]["projected_candidate_cost_microusd"],
            expensive["input"]["scenario"]["projected_original_cost_microusd"],
        )

        expected_error_bodies = {
            "401": '{"error":{"code":"invalid_api_key","message":"synthetic unauthorized","type":"authentication_error"}}',
            "429": '{"error":{"code":"rate_limit_exceeded","message":"synthetic rate limit","type":"rate_limit_error"}}',
            "500": '{"error":{"code":"internal_error","message":"synthetic provider failure","type":"server_error"}}',
        }
        for status in ("401", "429", "500"):
            record = development("protocol_and_isolation", status)
            scenario = record["input"]["scenario"]
            body = expected_error_bodies[status]
            self.assertEqual(scenario["status"], int(status))
            self.assertEqual(scenario["frames"], [])
            self.assertEqual(scenario["sse_frames_utf8"], [])
            self.assertEqual(scenario["provider_response_body_utf8"], body)
            self.assertEqual(
                scenario["provider_response_body_byte_count"], len(body.encode())
            )
            self.assertEqual(
                scenario["provider_response_body_sha256"], sha256_bytes(body.encode())
            )
            self.assertTrue(scenario["passthrough_required"])
            self.assertEqual(record["expected_outcome"]["provider_status"], int(status))
            self.assertFalse(record["expected_outcome"]["retry_allowed"])
            self.assertTrue(record["expected_outcome"]["passthrough_required"])
            self.assertEqual(
                record["expected_outcome"]["provider_body_sha256"],
                sha256_bytes(body.encode()),
            )
            self.assertEqual(
                set(record["expected_disposition_by_technique"].values()),
                {"decline_ineligible"},
            )
        cancelled = development("protocol_and_isolation", "cancelled_stream")
        cancelled_scenario = cancelled["input"]["scenario"]
        cancellation_path = (
            ROOT
            / "fixtures"
            / "compand"
            / "openai-responses"
            / "codex-cli-0.144.5"
            / "responses"
            / "cancellation.sse"
        )
        expected_cancelled_body = cancellation_path.read_bytes().decode("utf-8")
        expected_cancelled_frames = split_sse_frames(expected_cancelled_body)
        expected_cancelled_events = [
            frame.splitlines()[0].removeprefix("event: ")
            for frame in expected_cancelled_frames
        ]
        self.assertTrue(cancelled_scenario["stream_cancelled"])
        self.assertEqual(cancelled_scenario["frames"], expected_cancelled_events)
        self.assertEqual(
            cancelled_scenario["sse_frames_utf8"], expected_cancelled_frames
        )
        self.assertEqual(
            expected_cancelled_events,
            [
                "response.created",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
            ],
        )
        self.assertFalse(cancelled_scenario["terminal_event_present"])
        self.assertFalse(cancelled_scenario["usage_present"])
        self.assertNotIn("response.completed", cancelled_scenario["frames"])
        self.assertNotIn("response.cancelled", cancelled_scenario["frames"])
        self.assertEqual(
            cancelled_scenario["provider_response_body_utf8"],
            expected_cancelled_body,
        )
        self.assertEqual(
            cancelled_scenario["provider_response_body_utf8"].encode("utf-8"),
            cancellation_path.read_bytes(),
        )
        self.assertEqual(
            cancelled_scenario["provider_response_body_sha256"],
            sha256_bytes(expected_cancelled_body.encode()),
        )
        self.assertEqual(
            cancelled["expected_outcome"]["sse_frame_sha256s"],
            [sha256_bytes(frame.encode()) for frame in expected_cancelled_frames],
        )
        self.assertFalse(cancelled["expected_outcome"]["terminal_event_present"])
        self.assertFalse(cancelled["expected_outcome"]["usage_present"])
        self.assertTrue(cancelled["expected_outcome"]["passthrough_required"])
        self.assertEqual(
            set(cancelled["expected_disposition_by_technique"].values()),
            {"decline_ineligible"},
        )
        self.assertEqual(cancelled["expected_outcome"]["status"], "cancelled")
        retry = development("protocol_and_isolation", "retry_identity")
        attempts = retry["input"]["scenario"]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            {attempt["request_identity"] for attempt in attempts}, {"retry-fixture-1"}
        )
        self.assertTrue(retry["expected_outcome"]["retry_allowed"])
        continuation_modes = {
            "manual_continuation": "manual_replay",
            "previous_response_continuation": "previous_response_id",
            "conversation_continuation": "conversation_id",
        }
        for case_id, mode in continuation_modes.items():
            scenario = development("protocol_and_isolation", case_id)["input"][
                "scenario"
            ]
            self.assertEqual(scenario["continuation_mode"], mode)

    def test_hidden_holdout_fails_closed_without_leaking_payload_or_oracle(
        self,
    ) -> None:
        self.assertFalse(self.holdout["developer_visible"])
        self.assertFalse(self.holdout["payloads_in_repository"])
        self.assertFalse(self.holdout["attested"])
        self.assertEqual(
            self.holdout["custodian"], "independent_QA-58_holdout_custodian"
        )
        self.assertEqual(len(self.holdout["reservations"]), 11)
        self.assertEqual(
            {item["fixture_family"] for item in self.holdout["reservations"]},
            {item["id"] for item in self.manifest["fixture_families"]},
        )
        for reservation in self.holdout["reservations"]:
            self.assertEqual(reservation["partition"], "hidden_holdout")
            self.assertNotIn("input", reservation)
            self.assertNotIn("baseline_provider_view", reservation)
            self.assertNotIn("expected_outcome", reservation)
        hidden = self.index["partitions"]["hidden_holdout"]
        self.assertEqual(hidden["fixture_count"], 0)
        self.assertIsNone(hidden["root_sha256"])
        self.assertFalse(hidden["attested"])
        self.assertTrue(self.manifest["materialization"]["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()
