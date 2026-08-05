"""Pure CES-1 mechanical grading rules.

The grader consumes normalized evidence.  Technique plugins deliberately have no
dependency on this module and cannot influence gates, weights, publication state,
or Value Index movement.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any


class AblationArm(StrEnum):
    """The complete CES-1 arm vocabulary."""

    BASELINE = "B0"
    SHADOW = "S1"
    ENFORCED = "E1"
    COMBINATION = "C1"


HARD_GATE_IDS = (
    "correctness",
    "attribution",
    "isolation",
    "reproducibility",
    "protocol_safety",
    "exact_recovery",
    "fail_open",
    "whole_task_economics",
    "quality_noninferiority",
    "clean_environment_regeneration",
)

GRADE_WEIGHTS: dict[str, dict[str, int]] = {
    "technical": {
        "correctness": 35,
        "reproducibility": 20,
        "failure_transparency": 20,
        "latency_reliability": 15,
        "simplicity": 10,
    },
    "user_value": {
        "net_cost_per_verified_task": 40,
        "natural_eligible_spend_coverage": 25,
        "outcome_noninferiority": 25,
        "latency_friction": 10,
    },
    "company_value": {
        "defensible_evidence": 30,
        "margin_potential": 25,
        "cross_lane_applicability": 20,
        "operations_burden": 15,
        "ip_design_around_evidence": 10,
    },
    "asset_value": {
        "certified_reusable_profile": 30,
        "failure_and_rollback_corpus": 25,
        "cross_lane_calibration": 20,
        "reproducible_evidence_compiler": 15,
        "ip_prior_art_record": 10,
    },
}

KPI_IDS = (
    "compand.p2.net_cost_per_verified_task_usd",
    "compand.p2.natural_eligible_spend_coverage_ratio",
    "compand.p2.task_outcome_noninferiority_rate",
    "compand.p2.gateway_added_latency_p95_ms",
    "compand.p2.reliable_request_rate",
    "compand.p2.exact_recovery_success_rate",
)


def canonical_json_bytes(value: object) -> bytes:
    """Return the deterministic JSON encoding used by QA-57 evidence hashes."""

    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_hex(canonical_json_bytes(value))


def scorecard_sha256(scorecard: Mapping[str, Any]) -> str:
    """Hash a scorecard with RFC 8785 JCS after excluding its self-hash."""

    materialized = json.loads(json.dumps(scorecard, allow_nan=False))
    trace = materialized.get("trace")
    if not isinstance(trace, dict):
        raise ValueError("scorecard trace is required")
    trace.pop("scorecard_sha256", None)
    return sha256_hex(jcs_canonical_json_bytes(materialized))


def jcs_canonical_json_bytes(value: object) -> bytes:
    """Serialize I-JSON values with the RFC 8785 canonical form."""

    return _jcs_value(value).encode("utf-8")


def _jcs_value(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError("JCS integers must be exactly representable by IEEE-754")
        return str(value)
    if isinstance(value, float):
        return _jcs_number(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("JCS strings may not contain lone surrogates") from exc
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JCS object keys must be strings")
        try:
            keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        except UnicodeEncodeError as exc:
            raise ValueError("JCS object keys may not contain lone surrogates") from exc
        return (
            "{"
            + ",".join(f"{_jcs_value(key)}:{_jcs_value(value[key])}" for key in keys)
            + "}"
        )
    raise ValueError(f"unsupported JCS value type: {type(value).__name__}")


def _jcs_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("JCS numbers must be finite")
    if value == 0:
        return "0"
    negative = value < 0
    absolute = abs(value)
    rendered = repr(absolute).lower()
    if "e" not in rendered:
        if rendered.endswith(".0"):
            rendered = rendered[:-2]
        return ("-" if negative else "") + rendered
    mantissa, exponent_text = rendered.split("e", 1)
    exponent = int(exponent_text)
    digits = mantissa.replace(".", "")
    decimal_position = 1 + exponent
    if 1e-6 <= absolute < 1e21:
        if decimal_position <= 0:
            rendered = "0." + "0" * (-decimal_position) + digits
        elif decimal_position >= len(digits):
            rendered = digits + "0" * (decimal_position - len(digits))
        else:
            rendered = digits[:decimal_position] + "." + digits[decimal_position:]
        rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    else:
        normalized_mantissa = digits[0]
        if len(digits) > 1:
            normalized_mantissa += "." + digits[1:].rstrip("0")
            normalized_mantissa = normalized_mantissa.rstrip(".")
        rendered = (
            normalized_mantissa + "e" + ("+" if exponent >= 0 else "") + str(exponent)
        )
    return ("-" if negative else "") + rendered


def interval_95(
    values: Sequence[float],
) -> tuple[float | None, float | None, float | None]:
    """Return a transparent normal 95% interval for task-level paired values."""

    if not values:
        return None, None, None
    estimate = statistics.fmean(values)
    if len(values) == 1:
        return estimate, estimate, estimate
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    half_width = 1.96 * standard_error
    return estimate, estimate - half_width, estimate + half_width


def percentile(values: Sequence[float], quantile: float) -> float | None:
    """Return a deterministic linearly interpolated percentile."""

    if not values:
        return None
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def band_for(score: float, *, hard_gate_passed: bool) -> str:
    if not hard_gate_passed:
        return "F"
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def weighted_grade(
    grade_name: str,
    component_ratios: Mapping[str, int | float],
    *,
    hard_gate_passed: bool,
) -> dict[str, object]:
    """Mechanically apply frozen weights to component ratios in ``[0, 1]``."""

    try:
        weights = GRADE_WEIGHTS[grade_name]
    except KeyError as exc:
        raise ValueError(f"unknown grade: {grade_name}") from exc
    unknown = set(component_ratios) - set(weights)
    if unknown:
        raise ValueError(f"unknown {grade_name} components: {sorted(unknown)}")
    components: dict[str, float] = {}
    for name, weight in weights.items():
        raw = float(component_ratios.get(name, 0.0))
        if not math.isfinite(raw) or not 0 <= raw <= 1:
            raise ValueError(f"{grade_name}.{name} must be a finite ratio in [0, 1]")
        components[name] = round(raw * weight, 6)
    score = round(sum(components.values()), 6)
    return {
        "score": score,
        "band": band_for(score, hard_gate_passed=hard_gate_passed),
        "components": components,
    }


__all__ = [
    "AblationArm",
    "GRADE_WEIGHTS",
    "HARD_GATE_IDS",
    "KPI_IDS",
    "band_for",
    "canonical_json_bytes",
    "interval_95",
    "jcs_canonical_json_bytes",
    "percentile",
    "scorecard_sha256",
    "sha256_hex",
    "sha256_json",
    "weighted_grade",
]
