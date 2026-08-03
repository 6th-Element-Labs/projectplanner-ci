from __future__ import annotations

import hashlib
import json
import unittest
from datetime import date, datetime, timezone

from path_setup import ROOT  # noqa: F401 - adds src/ to sys.path
from switchboard.application.commands.compand_scan import (
    compile_gateway_coverage_receipt,
    decide_compand_scan,
    measure_line_rle_candidate,
)
from switchboard.contracts.compand import (
    CompandSystemSnapshot,
    DirectGatewayParity,
    EgressObservation,
    EgressObservationWindow,
    GatewayCoverageReceiptInput,
    ProviderPriceTable,
    ProviderTokenCount,
)
from switchboard.domain.compand import (
    ScanEligibilityError,
    build_line_rle_candidate,
    decode_line_rle,
)


def system_snapshot() -> CompandSystemSnapshot:
    return CompandSystemSnapshot(
        client_version="0.144.5",
        client_binary_sha256="sha256:" + "a" * 64,
        os_arch="Mac OS 26.3.0 arm64",
        model="gpt-5.4",
        provider_id="compand_fixture",
        provider_name="Compand Fixture Capture",
        provider_base_url="http://127.0.0.1:18765/v1",
        credential_environment_variable="COMPAND_FIXTURE_API_KEY",
        reasoning_effort="high",
        request_max_retries=0,
        stream_max_retries=0,
        stream_idle_timeout_ms=5000,
        gateway_version="DOGFOOD-32-test",
        task_snapshot_sha256="sha256:" + "b" * 64,
        configuration_sha256="sha256:" + "c" * 64,
    )


def parity(**changes: bool) -> DirectGatewayParity:
    values = {
        "protocol": True,
        "usage_fields": True,
        "task_result": True,
        "streaming": True,
        "tools": True,
        "errors": True,
        "cancellation": True,
    }
    values.update(changes)
    return DirectGatewayParity(**values)


def observation_window() -> EgressObservationWindow:
    return EgressObservationWindow(
        method="process_socket_audit",
        window_started_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        window_ended_at=datetime(2026, 8, 3, 0, 5, tzinfo=timezone.utc),
        observer_version="dogfood32-test",
        ancillary_destination_classes=("github",),
    )


def coverage_input(
    correlation_id: str,
    *,
    feature: str = "responses",
    endpoint: str = "/v1/responses",
    classification: str = "captured",
    mode: str = "scan",
) -> GatewayCoverageReceiptInput:
    return GatewayCoverageReceiptInput(
        correlation_id=correlation_id,
        client_version="0.144.5",
        mode=mode,
        certified_feature=feature,
        tuple_status="certified",
        observed_endpoint=endpoint,
        egress_classification=classification,
        source_version="DOGFOOD-32-test",
    )


def egress(
    correlation_id: str,
    *,
    endpoint: str = "/v1/responses",
    classification: str = "captured",
    reason: str = "certified_gateway_path",
) -> EgressObservation:
    return EgressObservation(
        correlation_id=correlation_id,
        method="POST",
        endpoint=endpoint,
        classification=classification,
        reason_code=reason,
    )


def full_receipt():
    inputs = [
        coverage_input("cmp_1", mode="passthrough"),
        coverage_input("cmp_2", mode="scan"),
        coverage_input(
            "cmp_models",
            feature="models",
            endpoint="/v1/models",
            classification="excluded",
        ),
    ]
    observations = [
        egress("cmp_1"),
        egress("cmp_2"),
        egress(
            "cmp_models",
            endpoint="/v1/models",
            classification="excluded",
            reason="control_endpoint",
        ),
    ]
    return compile_gateway_coverage_receipt(
        system=system_snapshot(),
        observation_window=observation_window(),
        parity=parity(),
        coverage_inputs=inputs,
        egress_observations=observations,
        exercised_features=("sse", "tools", "usage", "errors", "cancellation"),
    )


class Dogfood32CompandScanTest(unittest.TestCase):
    def test_coverage_receipt_reconciles_every_observation_and_is_stable(self) -> None:
        receipt = full_receipt()
        self.assertEqual(receipt.coverage, "full")
        self.assertFalse(receipt.mutation_blocked)
        self.assertFalse(receipt.direct_inference_egress_observed)
        self.assertEqual(receipt.coverage_counts.captured, 2)
        self.assertEqual(receipt.coverage_counts.excluded, 1)
        self.assertEqual(receipt.coverage_counts.total, 3)
        self.assertEqual(receipt.modes_exercised, ("passthrough", "scan"))
        self.assertIn("responses", receipt.certified_features)
        self.assertIn("cancellation", receipt.certified_features)
        self.assertTrue(receipt.evidence_hash.startswith("sha256:"))
        self.assertEqual(receipt, full_receipt())

    def test_missing_or_bypassed_egress_blocks_mutation_without_hiding_counts(self) -> None:
        missing = compile_gateway_coverage_receipt(
            system=system_snapshot(),
            observation_window=observation_window(),
            parity=parity(),
            coverage_inputs=[coverage_input("cmp_missing")],
            egress_observations=[],
        )
        self.assertEqual(missing.coverage, "unknown")
        self.assertEqual(missing.coverage_counts.unknown, 1)
        self.assertIn("unreconciled_egress_observation", missing.blocking_reasons)

        bypassed = compile_gateway_coverage_receipt(
            system=system_snapshot(),
            observation_window=observation_window(),
            parity=parity(),
            coverage_inputs=[coverage_input("cmp_captured")],
            egress_observations=[
                egress("cmp_captured"),
                egress(
                    "direct_1",
                    endpoint="https://api.openai.com/v1/responses",
                    classification="bypassed",
                    reason="direct_openai_egress",
                ),
            ],
        )
        self.assertEqual(bypassed.coverage, "partial")
        self.assertTrue(bypassed.direct_inference_egress_observed)
        self.assertEqual(bypassed.coverage_counts.bypassed, 1)
        self.assertIn("unexplained_bypass", bypassed.blocking_reasons)
        self.assertEqual(decide_compand_scan(bypassed, []).decision, "low_coverage_hold")

        fixture_only = compile_gateway_coverage_receipt(
            system=system_snapshot(),
            observation_window=observation_window().model_copy(
                update={"method": "fixture_loopback"}
            ),
            parity=parity(),
            coverage_inputs=[coverage_input("cmp_fixture")],
            egress_observations=[egress("cmp_fixture")],
        )
        self.assertEqual(fixture_only.coverage, "full")
        self.assertTrue(fixture_only.mutation_blocked)
        self.assertIn(
            "process_level_egress_observation_missing",
            fixture_only.blocking_reasons,
        )

    def test_line_rle_shadow_measurement_is_reversible_and_content_free(self) -> None:
        original = "private-alpha\nprivate-line\nprivate-line\nprivate-line\nprivate-omega\n"
        output_sha = hashlib.sha256(original.encode()).hexdigest()
        command_receipt = {
            "schema": "compand.command_result.v1",
            "source_kind": "command_result",
            "trusted_adapter": True,
            "exit_status": 0,
            "content_type": "text/plain",
            "encoding": "utf-8",
            "truncated": False,
            "signed": False,
            "new_suffix": True,
            "output": original,
            "byte_count": len(original.encode()),
            "output_sha256": output_sha,
        }
        candidate = build_line_rle_candidate(command_receipt)
        self.assertEqual(decode_line_rle(candidate.candidate_text), original)
        self.assertEqual(candidate.repeated_span_count, 1)
        self.assertEqual(candidate.repeated_line_count, 3)
        self.assertEqual(candidate.removed_line_count, 2)
        self.assertNotIn(original, repr(candidate))

        prices = ProviderPriceTable(
            provider="openai",
            model="gpt-5.4",
            effective_date=date(2026, 8, 3),
            input_usd_per_million_tokens=10,
            cached_input_usd_per_million_tokens=1,
            source="dated-test-table",
        )
        measurement = measure_line_rle_candidate(
            candidate,
            run_id="run-natural-1",
            task_snapshot_sha256="sha256:" + "b" * 64,
            original_count=ProviderTokenCount(
                input_tokens=20, cached_input_tokens=10, count_call_latency_ms=12
            ),
            candidate_count=ProviderTokenCount(
                input_tokens=10, cached_input_tokens=5, count_call_latency_ms=11
            ),
            price_table=prices,
            gateway_latency_ms=4,
            gateway_retry_count=0,
            task_completed=True,
            shadow_original_forwarded_byte_for_byte=True,
        )
        self.assertTrue(measurement.cache_adjusted_candidate_is_cheaper)
        self.assertGreater(measurement.projected_input_savings_usd, 0)
        serialized = json.dumps(measurement.model_dump(mode="json", by_alias=True))
        self.assertNotIn("private-alpha", serialized)
        self.assertNotIn("private-line", serialized)
        self.assertNotIn("private-omega", serialized)

        decision = decide_compand_scan(full_receipt(), [measurement])
        self.assertEqual(decision.decision, "advance")
        self.assertFalse(decision.mutation_authorized)

    def test_line_rle_oracle_escapes_literal_marker_lines(self) -> None:
        original = "literal [repeated 2 times]\nliteral [repeated 1 times]\n"
        receipt = {
            "schema": "compand.command_result.v1",
            "source_kind": "command_result",
            "trusted_adapter": True,
            "exit_status": 0,
            "content_type": "text/plain",
            "encoding": "utf-8",
            "truncated": False,
            "signed": False,
            "new_suffix": True,
            "output": original,
            "byte_count": len(original.encode()),
            "output_sha256": hashlib.sha256(original.encode()).hexdigest(),
        }
        candidate = build_line_rle_candidate(receipt)
        self.assertNotEqual(candidate.candidate_text, original)
        self.assertEqual(decode_line_rle(candidate.candidate_text), original)

    def test_absent_cache_fields_cannot_be_called_cache_adjusted_savings(self) -> None:
        original = "same\nsame\n"
        candidate = build_line_rle_candidate(
            {
                "schema": "compand.command_result.v1",
                "source_kind": "command_result",
                "trusted_adapter": True,
                "exit_status": 0,
                "content_type": "text/plain",
                "encoding": "utf-8",
                "truncated": False,
                "signed": False,
                "new_suffix": True,
                "output": original,
                "byte_count": len(original),
                "output_sha256": hashlib.sha256(original.encode()).hexdigest(),
            }
        )
        prices = ProviderPriceTable(
            provider="openai",
            model="gpt-5.4",
            effective_date=date(2026, 8, 3),
            input_usd_per_million_tokens=10,
            cached_input_usd_per_million_tokens=1,
            source="dated-test-table",
        )
        measurement = measure_line_rle_candidate(
            candidate,
            run_id="run-no-cache-fields",
            task_snapshot_sha256="sha256:" + "b" * 64,
            original_count=ProviderTokenCount(
                input_tokens=20, count_call_latency_ms=1
            ),
            candidate_count=ProviderTokenCount(
                input_tokens=5, count_call_latency_ms=1
            ),
            price_table=prices,
            gateway_latency_ms=1,
            gateway_retry_count=0,
            task_completed=True,
            shadow_original_forwarded_byte_for_byte=True,
        )
        self.assertFalse(measurement.cache_fields_exposed)
        self.assertFalse(measurement.cache_adjusted_candidate_is_cheaper)
        self.assertEqual(
            decide_compand_scan(full_receipt(), [measurement]).decision, "redesign"
        )

    def test_untrusted_or_unbound_command_output_is_ineligible(self) -> None:
        with self.assertRaisesRegex(ScanEligibilityError, "trusted_adapter"):
            build_line_rle_candidate(
                {
                    "schema": "compand.command_result.v1",
                    "source_kind": "command_result",
                    "trusted_adapter": False,
                }
            )


if __name__ == "__main__":
    unittest.main()
