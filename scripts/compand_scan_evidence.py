#!/usr/bin/env python3
"""Compile one sanitized DOGFOOD-32 Scan input into immutable evidence JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from switchboard.application.commands.compand_scan import (  # noqa: E402
    compile_gateway_coverage_receipt,
    decide_compand_scan,
)
from switchboard.contracts.compand import (  # noqa: E402
    CompandSystemSnapshot,
    DirectGatewayParity,
    EgressObservation,
    EgressObservationWindow,
    GatewayCoverageReceiptInput,
    LineRleShadowMeasurement,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to replace immutable evidence: {args.output}")

    raw = args.input.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        parser.error("input must decode to an object")
    receipt = compile_gateway_coverage_receipt(
        system=CompandSystemSnapshot.model_validate(payload.get("system")),
        observation_window=EgressObservationWindow.model_validate(
            payload.get("observation_window")
        ),
        parity=DirectGatewayParity.model_validate(payload.get("parity")),
        coverage_inputs=[
            GatewayCoverageReceiptInput.model_validate(item)
            for item in payload.get("coverage_inputs", [])
        ],
        egress_observations=[
            EgressObservation.model_validate(item)
            for item in payload.get("egress_observations", [])
        ],
        exercised_features=payload.get("exercised_features", []),
    )
    measurements = tuple(
        LineRleShadowMeasurement.model_validate(item)
        for item in payload.get("measurements", [])
    )
    decision = decide_compand_scan(receipt, measurements)
    result = {
        "schema": "compand.scan_evidence_bundle.v1",
        "evidence_state": payload.get("evidence_state", "exploratory"),
        "claim_limit": payload.get("claim_limit", "diagnostic_only"),
        "source_input_sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "coverage_receipt": receipt.model_dump(mode="json", by_alias=True),
        "measurements": [
            item.model_dump(mode="json", by_alias=True) for item in measurements
        ],
        "decision": decision.model_dump(mode="json", by_alias=True),
        "limitations": list(payload.get("limitations", [])),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
