#!/usr/bin/env python3
"""Executable reader/evaluator for the ADAPTER-12 runtime wake matrix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "switchboard.runtime_wake_capabilities.v1"
CAPABILITIES = (
    "active_message_delivery",
    "wake_intent",
    "start_runtime",
    "same_session_resume",
    "snapshot_kill_restart",
    "startup_inbox_drain",
)
SUPPORT_VALUES = {"conditional", "unsupported"}
CONTINUITY_MODES = {
    "exact_vendor_session",
    "checkpoint_resume",
    "reconstructed_history",
    "fresh_switchboard_state",
}
CONTINUITY_POLICIES = {"resume_required", "resume_preferred", "fresh_only"}
DEFAULT_MATRIX_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "runtime_wake_capabilities.v1.json"
)


def load_matrix(path: str | Path = DEFAULT_MATRIX_PATH) -> dict[str, Any]:
    """Load and structurally validate the checked-in capability contract."""
    matrix = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_matrix(matrix)
    if errors:
        raise ValueError("invalid runtime wake matrix: " + "; ".join(errors))
    return matrix


def validate_matrix(matrix: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if matrix.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")

    vocab = matrix.get("status_vocabularies") or {}
    if set(vocab.get("delivery_status") or []) != {
        "active", "unreachable", "identity_unbound"
    }:
        errors.append("delivery_status vocabulary is incomplete")
    if set(vocab.get("wake_status") or []) != {
        "pending", "claimed", "completed", "failed", "cancelled"
    }:
        errors.append("wake_status vocabulary is incomplete")
    if set(vocab.get("continuity_mode") or []) != CONTINUITY_MODES:
        errors.append("continuity_mode vocabulary is incomplete")
    if set(vocab.get("continuity_policy") or []) != CONTINUITY_POLICIES:
        errors.append("continuity_policy vocabulary is incomplete")
    if not matrix.get("security_invariants"):
        errors.append("security_invariants must not be empty")

    runtimes = matrix.get("runtimes") or []
    seen: set[str] = set()
    for runtime in runtimes:
        runtime_id = str(runtime.get("id") or "")
        if not runtime_id or runtime_id in seen:
            errors.append(f"runtime id is missing or duplicated: {runtime_id!r}")
        seen.add(runtime_id)
        if not runtime.get("adapter_setup"):
            errors.append(f"{runtime_id}: adapter_setup must not be empty")
        if not runtime.get("security_constraints"):
            errors.append(f"{runtime_id}: security_constraints must not be empty")
        capabilities = runtime.get("capabilities") or {}
        if set(capabilities) != set(CAPABILITIES):
            errors.append(f"{runtime_id}: capability set must match the contract")
            continue
        for capability_name, capability in capabilities.items():
            support = capability.get("support")
            if support not in SUPPORT_VALUES:
                errors.append(
                    f"{runtime_id}.{capability_name}: invalid support {support!r}"
                )
            if support == "conditional" and not capability.get("requires"):
                errors.append(
                    f"{runtime_id}.{capability_name}: conditional support needs requirements"
                )
            missing = capability.get("missing_setup") or {}
            if missing.get("allowed") is not False or not missing.get("reason"):
                errors.append(
                    f"{runtime_id}.{capability_name}: missing setup must fail closed"
                )
            continuity_mode = (capability.get("when_ready") or {}).get(
                "continuity_mode"
            )
            if continuity_mode and continuity_mode not in CONTINUITY_MODES:
                errors.append(
                    f"{runtime_id}.{capability_name}: invalid continuity mode"
                )
            if capability_name == "start_runtime" and support == "conditional" and (
                continuity_mode != "fresh_switchboard_state"
            ):
                errors.append(
                    f"{runtime_id}.start_runtime: fresh start must name its continuity"
                )
    if not runtimes:
        errors.append("runtimes must not be empty")
    return errors


def evaluate_capability(
    runtime_id: str,
    capability_name: str,
    available_requirements: Iterable[str],
    *,
    matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an allow/deny decision without inferring unavailable host/runtime state."""
    matrix = matrix or load_matrix()
    runtime = next(
        (item for item in matrix["runtimes"] if item["id"] == runtime_id), None
    )
    if runtime is None:
        return {
            "allowed": False,
            "reason": "unknown_runtime",
            "failure_class": "invalid_input",
        }
    if capability_name not in CAPABILITIES:
        return {
            "allowed": False,
            "reason": "unknown_capability",
            "failure_class": "invalid_input",
        }

    capability = runtime["capabilities"][capability_name]
    if capability["support"] == "unsupported":
        return {
            **capability["missing_setup"],
            "runtime": runtime_id,
            "capability": capability_name,
            "missing": [],
        }

    available = set(available_requirements)
    missing = sorted(set(capability["requires"]) - available)
    if missing:
        return {
            **capability["missing_setup"],
            "runtime": runtime_id,
            "capability": capability_name,
            "missing": missing,
        }
    return {
        "allowed": True,
        "runtime": runtime_id,
        "capability": capability_name,
        "missing": [],
        **capability.get("when_ready", {}),
    }
