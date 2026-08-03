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
    LineRleShadowMeasurement,
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


def qualifying_measurement() -> LineRleShadowMeasurement:
    original = "same\nsame\n"
    candidate = build_line_rle_candidate(
        {
            "schema": "compand.command_result.v1",
            "call_id": "call-qualifying",
            "source_kind": "command_result",
            "trusted_adapter": True,
            "exit_status": 0,
            "content_type": "text/plain",
            "encoding": "utf-8",
            "truncated": False,
            "signed": False,
            "new_suffix": True,
            "byte_count": len(original.encode()),
            "output_sha256": hashlib.sha256(original.encode()).hexdigest(),
        },
        expected_call_id="call-qualifying",
        output_item={
            "type": "function_call_output",
            "call_id": "call-qualifying",
            "output": original,
        },
    )
    return measure_line_rle_candidate(
        candidate,
        run_id="run-qualifying",
        task_snapshot_sha256="sha256:" + "b" * 64,
        original_count=ProviderTokenCount(
            input_tokens=20,
            cached_input_tokens=10,
            count_call_latency_ms=1,
        ),
        candidate_count=ProviderTokenCount(
            input_tokens=5,
            cached_input_tokens=0,
            count_call_latency_ms=1,
        ),
        price_table=ProviderPriceTable(
            provider="compand_fixture",
            model="gpt-5.4",
            effective_date=date(2026, 8, 3),
            input_usd_per_million_tokens=10,
            cached_input_usd_per_million_tokens=1,
            source="dated-test-table",
        ),
        gateway_latency_ms=1,
        gateway_retry_count=0,
        task_completed=True,
        shadow_original_forwarded_byte_for_byte=True,
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

    def test_advance_requires_a_captured_responses_inference_route(self) -> None:
        measurement = qualifying_measurement()
        control_only = compile_gateway_coverage_receipt(
            system=system_snapshot(),
            observation_window=observation_window(),
            parity=parity(),
            coverage_inputs=[
                coverage_input(
                    "cmp_models_only",
                    feature="models",
                    endpoint="/v1/models",
                    classification="excluded",
                )
            ],
            egress_observations=[
                egress(
                    "cmp_models_only",
                    endpoint="/v1/models",
                    classification="excluded",
                    reason="control_endpoint",
                )
            ],
        )

        self.assertEqual(control_only.coverage, "control_only")
        self.assertEqual(control_only.coverage_counts.captured, 0)
        self.assertTrue(control_only.mutation_blocked)
        self.assertIn(
            "no_captured_inference_requests", control_only.blocking_reasons
        )
        self.assertIn(
            "captured_responses_route_missing", control_only.blocking_reasons
        )
        control_decision = decide_compand_scan(control_only, [measurement])
        self.assertEqual(control_decision.decision, "low_coverage_hold")
        self.assertEqual(control_decision.qualifying_candidate_count, 0)
        self.assertFalse(control_decision.mutation_authorized)

        count_only = compile_gateway_coverage_receipt(
            system=system_snapshot(),
            observation_window=observation_window(),
            parity=parity(),
            coverage_inputs=[
                coverage_input(
                    "cmp_count_only",
                    feature="input_tokens",
                    endpoint="/v1/responses/input_tokens",
                )
            ],
            egress_observations=[
                egress(
                    "cmp_count_only",
                    endpoint="/v1/responses/input_tokens",
                )
            ],
        )

        self.assertEqual(count_only.coverage, "full")
        self.assertNotIn(
            "no_captured_inference_requests", count_only.blocking_reasons
        )
        self.assertIn(
            "captured_responses_route_missing", count_only.blocking_reasons
        )
        count_decision = decide_compand_scan(count_only, [measurement])
        self.assertEqual(count_decision.decision, "low_coverage_hold")
        self.assertEqual(count_decision.qualifying_candidate_count, 0)

    def test_line_rle_shadow_measurement_is_reversible_and_content_free(self) -> None:
        original = "private-alpha\nprivate-line\nprivate-line\nprivate-line\nprivate-omega\n"
        output_sha = hashlib.sha256(original.encode()).hexdigest()
        command_receipt = {
            "schema": "compand.command_result.v1",
            "call_id": "call-natural-1",
            "source_kind": "command_result",
            "trusted_adapter": True,
            "exit_status": 0,
            "content_type": "text/plain",
            "encoding": "utf-8",
            "truncated": False,
            "signed": False,
            "new_suffix": True,
            "byte_count": len(original.encode()),
            "output_sha256": output_sha,
        }
        candidate = build_line_rle_candidate(
            command_receipt,
            expected_call_id="call-natural-1",
            output_item={
                "type": "function_call_output",
                "call_id": "call-natural-1",
                "output": original,
            },
        )
        self.assertEqual(decode_line_rle(candidate.candidate_text), original)
        self.assertEqual(candidate.repeated_span_count, 1)
        self.assertEqual(candidate.repeated_line_count, 3)
        self.assertEqual(candidate.removed_line_count, 2)
        self.assertNotIn(original, repr(candidate))

        prices = ProviderPriceTable(
            provider="compand_fixture",
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

        wrong_provider = measurement.model_copy(
            update={
                "price_table": prices.model_copy(update={"provider": "other-provider"})
            }
        )
        provider_decision = decide_compand_scan(full_receipt(), [wrong_provider])
        self.assertEqual(provider_decision.decision, "stop")
        self.assertIn("measurement_provider_mismatch", provider_decision.reasons)

        wrong_model = measurement.model_copy(
            update={"price_table": prices.model_copy(update={"model": "other-model"})}
        )
        model_decision = decide_compand_scan(full_receipt(), [wrong_model])
        self.assertEqual(model_decision.decision, "stop")
        self.assertIn("measurement_model_mismatch", model_decision.reasons)

    def test_line_rle_oracle_escapes_literal_marker_lines(self) -> None:
        original = "literal [repeated 2 times]\nliteral [repeated 1 times]\n"
        receipt = {
            "schema": "compand.command_result.v1",
            "call_id": "call-marker",
            "source_kind": "command_result",
            "trusted_adapter": True,
            "exit_status": 0,
            "content_type": "text/plain",
            "encoding": "utf-8",
            "truncated": False,
            "signed": False,
            "new_suffix": True,
            "byte_count": len(original.encode()),
            "output_sha256": hashlib.sha256(original.encode()).hexdigest(),
        }
        candidate = build_line_rle_candidate(
            receipt,
            expected_call_id="call-marker",
            output_item={
                "type": "function_call_output",
                "call_id": "call-marker",
                "output": original,
            },
        )
        self.assertNotEqual(candidate.candidate_text, original)
        self.assertEqual(decode_line_rle(candidate.candidate_text), original)

    def test_absent_cache_fields_cannot_be_called_cache_adjusted_savings(self) -> None:
        original = "same\nsame\n"
        candidate = build_line_rle_candidate(
            {
                "schema": "compand.command_result.v1",
                "call_id": "call-no-cache",
                "source_kind": "command_result",
                "trusted_adapter": True,
                "exit_status": 0,
                "content_type": "text/plain",
                "encoding": "utf-8",
                "truncated": False,
                "signed": False,
                "new_suffix": True,
                "byte_count": len(original),
                "output_sha256": hashlib.sha256(original.encode()).hexdigest(),
            },
            expected_call_id="call-no-cache",
            output_item={
                "type": "function_call_output",
                "call_id": "call-no-cache",
                "output": original,
            },
        )
        prices = ProviderPriceTable(
            provider="compand_fixture",
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
                    "call_id": "call-untrusted",
                    "source_kind": "command_result",
                    "trusted_adapter": False,
                },
                expected_call_id="call-untrusted",
                output_item={
                    "type": "function_call_output",
                    "call_id": "call-untrusted",
                    "output": "ignored",
                },
            )

    def test_command_receipt_requires_exact_new_output_call_id_binding(self) -> None:
        output = "same\nsame\n"
        base = {
            "schema": "compand.command_result.v1",
            "source_kind": "command_result",
            "trusted_adapter": True,
            "exit_status": 0,
            "content_type": "text/plain",
            "encoding": "utf-8",
            "truncated": False,
            "signed": False,
            "new_suffix": True,
            "byte_count": len(output.encode()),
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        }
        item = {
            "type": "function_call_output",
            "call_id": "call-bound",
            "output": output,
        }
        with self.assertRaisesRegex(ScanEligibilityError, "expected_call_id is required"):
            build_line_rle_candidate(
                {**base, "call_id": "call-bound"},
                expected_call_id="",
                output_item=item,
            )
        with self.assertRaisesRegex(ScanEligibilityError, "receipt call_id is required"):
            build_line_rle_candidate(
                base,
                expected_call_id="call-bound",
                output_item=item,
            )
        with self.assertRaisesRegex(ScanEligibilityError, "receipt call_id does not match"):
            build_line_rle_candidate(
                {**base, "call_id": "call-other"},
                expected_call_id="call-bound",
                output_item=item,
            )
        with self.assertRaisesRegex(
            ScanEligibilityError, "function_call_output call_id does not match"
        ):
            build_line_rle_candidate(
                {**base, "call_id": "call-bound"},
                expected_call_id="call-bound",
                output_item={**item, "call_id": "call-other"},
            )
        with self.assertRaisesRegex(ScanEligibilityError, "output_sha256 does not bind"):
            build_line_rle_candidate(
                {
                    **base,
                    "call_id": "call-bound",
                    "output_sha256": "0" * 64,
                },
                expected_call_id="call-bound",
                output_item=item,
            )

    def test_equal_token_counts_cannot_advance_with_forged_derived_values(self) -> None:
        prices = ProviderPriceTable(
            provider="compand_fixture",
            model="gpt-5.4",
            effective_date=date(2026, 8, 3),
            input_usd_per_million_tokens=10,
            cached_input_usd_per_million_tokens=1,
            source="dated-test-table",
        )
        count = ProviderTokenCount(
            input_tokens=20,
            cached_input_tokens=10,
            count_call_latency_ms=1,
        )
        forged = LineRleShadowMeasurement.model_construct(
            run_id="run-forged-equal-counts",
            task_snapshot_sha256="sha256:" + "b" * 64,
            source_artifact_sha256="sha256:" + "d" * 64,
            candidate_artifact_sha256="sha256:" + "e" * 64,
            repeated_span_count=1,
            repeated_line_count=2,
            removed_line_count=1,
            original_bytes=10,
            candidate_bytes=8,
            original_count=count,
            candidate_count=count,
            cache_fields_exposed=True,
            projected_original_input_usd=0.00011,
            projected_candidate_input_usd=0.00001,
            projected_input_savings_usd=0.0001,
            cache_adjusted_candidate_is_cheaper=True,
            gateway_latency_ms=1,
            gateway_retry_count=0,
            task_completed=True,
            shadow_original_forwarded_byte_for_byte=True,
            price_table=prices,
        )
        with self.assertRaisesRegex(ValueError, "derived economics mismatch"):
            LineRleShadowMeasurement.model_validate(
                forged.model_dump(mode="json", by_alias=True)
            )
        decision = decide_compand_scan(full_receipt(), [forged])
        self.assertEqual(decision.decision, "stop")
        self.assertEqual(decision.qualifying_candidate_count, 0)
        self.assertTrue(
            any(
                reason.startswith("measurement_derived_value_mismatch:")
                for reason in decision.reasons
            )
        )

    def test_cached_tokens_above_total_fail_closed_when_validation_was_bypassed(
        self,
    ) -> None:
        prices = ProviderPriceTable(
            provider="compand_fixture",
            model="gpt-5.4",
            effective_date=date(2026, 8, 3),
            input_usd_per_million_tokens=10,
            cached_input_usd_per_million_tokens=1,
            source="dated-test-table",
        )
        invalid_original = ProviderTokenCount.model_construct(
            input_tokens=5,
            cached_input_tokens=10,
            count_call_latency_ms=1,
            retry_count=0,
            source="provider_input_tokens",
        )
        candidate = ProviderTokenCount(
            input_tokens=1,
            cached_input_tokens=0,
            count_call_latency_ms=1,
        )
        forged = LineRleShadowMeasurement.model_construct(
            run_id="run-invalid-cached-count",
            task_snapshot_sha256="sha256:" + "b" * 64,
            source_artifact_sha256="sha256:" + "d" * 64,
            candidate_artifact_sha256="sha256:" + "e" * 64,
            repeated_span_count=1,
            repeated_line_count=2,
            removed_line_count=1,
            original_bytes=10,
            candidate_bytes=8,
            original_count=invalid_original,
            candidate_count=candidate,
            cache_fields_exposed=True,
            projected_original_input_usd=-0.00004,
            projected_candidate_input_usd=0.00001,
            projected_input_savings_usd=-0.00005,
            cache_adjusted_candidate_is_cheaper=False,
            gateway_latency_ms=1,
            gateway_retry_count=0,
            task_completed=True,
            shadow_original_forwarded_byte_for_byte=True,
            price_table=prices,
        )

        decision = decide_compand_scan(full_receipt(), [forged])

        self.assertEqual(decision.decision, "stop")
        self.assertEqual(decision.qualifying_candidate_count, 0)
        self.assertIn("measurement_primitive_evidence_invalid", decision.reasons)

    def test_impossible_candidate_primitives_cannot_create_promotion_authority(
        self,
    ) -> None:
        valid = qualifying_measurement()
        impossible_values = {
            "repeated_span_count": 1,
            "repeated_line_count": 0,
            "removed_line_count": 0,
            "original_bytes": 0,
            "candidate_bytes": 0,
        }
        impossible_payload = {
            **valid.model_dump(mode="json", by_alias=True),
            **impossible_values,
        }

        with self.assertRaisesRegex(
            ValueError, "line-rle-v1 artifact evidence must be non-empty"
        ):
            LineRleShadowMeasurement.model_validate(impossible_payload)

        bypassed = valid.model_copy(update=impossible_values)
        decision = decide_compand_scan(full_receipt(), [bypassed])
        self.assertEqual(decision.decision, "stop")
        self.assertEqual(decision.qualifying_candidate_count, 0)
        self.assertIn("measurement_primitive_evidence_invalid", decision.reasons)

        for changes, message in (
            (
                {"repeated_line_count": 1, "removed_line_count": 0},
                "each repeated span must attest at least two repeated lines",
            ),
            (
                {"repeated_line_count": 3, "removed_line_count": 1},
                "removed_line_count must equal repeated lines minus repeated spans",
            ),
        ):
            with self.subTest(changes=changes):
                structurally_impossible = {
                    **valid.model_dump(mode="json", by_alias=True),
                    **changes,
                }
                with self.assertRaisesRegex(ValueError, message):
                    LineRleShadowMeasurement.model_validate(structurally_impossible)

    def test_malformed_candidate_artifact_hash_is_rejected(self) -> None:
        payload = qualifying_measurement().model_dump(mode="json", by_alias=True)
        payload["candidate_artifact_sha256"] = "not-a-content-address"

        with self.assertRaisesRegex(
            ValueError, "candidate_artifact_sha256 is not canonical sha256 evidence"
        ):
            LineRleShadowMeasurement.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
