"""Effective Agent Host runtime-profile identity and drift evaluation.

The profile is deliberately small and non-secret.  It fingerprints only the
configuration facts that decide whether a host can finish a task: effective
runtime work modules, automatic Work Session provisioning, Agent Host build,
critical binary presence, and the host-proven Watch relay capability.

This is admission control, not configuration management.  Hosts advertise what
is true; coordinators describe the subset they require and reject mismatches.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


RUNTIME_PROFILE_SCHEMA = "switchboard.agent_host_runtime_profile.v1"
RUNTIME_PROFILE_REQUIREMENT_SCHEMA = "switchboard.runtime_profile_requirement.v1"
RUNTIME_PROFILE_VERSION = 1

EXPECTED_WORK_MODULES = {
    "codex": "adapters.codex_local_worker:run",
    # Fleet PYTHONPATH includes adapters/, so the deployed Claude worker's
    # schema-of-record value is intentionally the unprefixed module name.
    "claude-code": "claude_personal_worker:run",
}
RUNTIME_BINARIES = {
    "codex": "codex",
    "claude-code": "claude",
    "cursor": "cursor",
}


def runtime_env_key(runtime: str) -> str:
    suffix = re.sub(r"[^A-Z0-9]+", "_", str(runtime or "").upper()).strip("_")
    return f"PM_AGENT_WORK_MODULE_{suffix}" if suffix else "PM_AGENT_WORK_MODULE"


def _canonical_hash(components: Mapping[str, Any], profile_version: int) -> str:
    try:
        normalized_version: Any = int(profile_version)
    except (TypeError, ValueError):
        # Malformed remote advertisements must produce a typed mismatch, never
        # crash the placement tick that is trying to reject them.
        normalized_version = str(profile_version or "")
    payload = {
        "profile_version": normalized_version,
        "components": dict(components or {}),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _labels_for(runtimes: list[str]) -> dict[str, str]:
    labels = {
        "auto_work_session": "PM_AUTO_WORK_SESSION",
        "agent_host_version": "agent_host_version",
        "binaries.git": "binary:git",
        "binaries.gh": "binary:gh",
        "relay.runner_watch": "runner_watch",
    }
    for runtime in runtimes:
        labels[f"work_modules.{runtime}"] = runtime_env_key(runtime)
        binary = RUNTIME_BINARIES.get(runtime)
        if binary:
            labels[f"binaries.{binary}"] = f"binary:{binary}"
    return labels


def build_runtime_profile(*, runtimes: list[str], work_modules: Mapping[str, str],
                          auto_work_session: bool, agent_host_version: str,
                          binaries: Mapping[str, bool], runner_watch: bool) -> dict[str, Any]:
    """Build the canonical, hash-stable host advertisement."""
    normalized_runtimes = sorted({str(item or "").strip() for item in runtimes
                                  if str(item or "").strip()})
    components = {
        "work_modules": {
            runtime: str((work_modules or {}).get(runtime) or "").strip()
            for runtime in normalized_runtimes
        },
        "auto_work_session": bool(auto_work_session),
        "agent_host_version": str(agent_host_version or "").strip(),
        "binaries": {
            str(name): bool(present)
            for name, present in sorted((binaries or {}).items())
            if str(name or "").strip()
        },
        "relay": {"runner_watch": bool(runner_watch)},
    }
    return {
        "schema": RUNTIME_PROFILE_SCHEMA,
        "profile_version": RUNTIME_PROFILE_VERSION,
        "hash": _canonical_hash(components, RUNTIME_PROFILE_VERSION),
        "components": components,
        "labels": _labels_for(normalized_runtimes),
    }


def runtime_profile_requirement(runtime: str, *, session_policy: str = "",
                                require_runner_watch: bool = False,
                                agent_host_version: str = "",
                                expected_profile_hash: str = "") -> dict[str, Any]:
    """Describe the coordinator's required effective profile for one runtime.

    Agent Host version and an exact full-profile hash are optional rollout
    fences.  The task-finishing components are enforced by default so a missing
    ``gh`` or wrong Codex work module is rejected even before a version fence is
    configured.
    """
    runtime = str(runtime or "").strip().lower()
    components: dict[str, Any] = {
        "binaries": {"git": True, "gh": True},
    }
    work_module = EXPECTED_WORK_MODULES.get(runtime)
    if work_module:
        components["work_modules"] = {runtime: work_module}
    runtime_binary = RUNTIME_BINARIES.get(runtime)
    if runtime_binary:
        components["binaries"][runtime_binary] = True
    if str(session_policy or "").strip() == "code_strict":
        components["auto_work_session"] = True
    if require_runner_watch:
        components["relay"] = {"runner_watch": True}
    if str(agent_host_version or "").strip():
        components["agent_host_version"] = str(agent_host_version).strip()
    requirement = {
        "schema": RUNTIME_PROFILE_REQUIREMENT_SCHEMA,
        "profile_version": RUNTIME_PROFILE_VERSION,
        "components": components,
        "labels": _labels_for([runtime] if runtime else []),
    }
    if str(expected_profile_hash or "").strip():
        requirement["expected_profile_hash"] = str(expected_profile_hash).strip()
    return requirement


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flattened: dict[str, Any] = {}
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten(child, path))
        return flattened
    return {prefix: value}


def _display(value: Any) -> str:
    if value is _MISSING:
        return "<missing>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


_MISSING = object()


def evaluate_runtime_profile(profile: Any, requirement: Any) -> dict[str, Any]:
    """Compare one advertisement with an expected subset and name every drift."""
    requirement = dict(requirement or {}) if isinstance(requirement, Mapping) else {}
    if not requirement:
        return {
            "eligible": True,
            "reason_code": None,
            "advertised_hash": (profile or {}).get("hash") if isinstance(profile, Mapping) else None,
            "mismatches": [],
        }

    mismatches: list[dict[str, Any]] = []
    labels = dict(requirement.get("labels") or {})
    if not isinstance(profile, Mapping):
        mismatches.append({
            "key": "runtime_profile",
            "label": "runtime_profile",
            "expected": "advertised",
            "actual": "<missing>",
            "reason": "profile drift: runtime_profile=<missing> (expected advertised)",
        })
        return {
            "eligible": False,
            "reason_code": "runtime_profile_missing",
            "advertised_hash": None,
            "mismatches": mismatches,
        }

    profile = dict(profile)
    profile_version = profile.get("profile_version")
    expected_version = requirement.get("profile_version", RUNTIME_PROFILE_VERSION)
    if profile.get("schema") != RUNTIME_PROFILE_SCHEMA or profile_version != expected_version:
        actual = f"{profile.get('schema') or '<missing>'}@{_display(profile_version)}"
        expected = f"{RUNTIME_PROFILE_SCHEMA}@{expected_version}"
        mismatches.append({
            "key": "profile_version",
            "label": "runtime_profile_version",
            "expected": expected,
            "actual": actual,
            "reason": f"profile drift: runtime_profile_version={actual} (expected {expected})",
        })

    components = profile.get("components")
    if not isinstance(components, Mapping):
        components = {}
    advertised_hash = str(profile.get("hash") or "")
    calculated_hash = _canonical_hash(components, profile_version or RUNTIME_PROFILE_VERSION)
    if advertised_hash != calculated_hash:
        mismatches.append({
            "key": "profile_hash",
            "label": "runtime_profile_hash",
            "expected": calculated_hash,
            "actual": advertised_hash or "<missing>",
            "reason": (
                "profile drift: runtime_profile_hash="
                f"{advertised_hash or '<missing>'} (expected {calculated_hash})"
            ),
        })

    expected_hash = str(requirement.get("expected_profile_hash") or "").strip()
    if expected_hash and advertised_hash != expected_hash:
        mismatches.append({
            "key": "expected_profile_hash",
            "label": "runtime_profile_hash",
            "expected": expected_hash,
            "actual": advertised_hash or "<missing>",
            "reason": (
                "profile drift: runtime_profile_hash="
                f"{advertised_hash or '<missing>'} (expected {expected_hash})"
            ),
        })

    actual_flat = _flatten(components)
    for key, expected in _flatten(requirement.get("components") or {}).items():
        actual = actual_flat.get(key, _MISSING)
        if actual == expected:
            continue
        label = str(labels.get(key) or (profile.get("labels") or {}).get(key) or key)
        mismatch = {
            "key": key,
            "label": label,
            "expected": expected,
            "actual": None if actual is _MISSING else actual,
        }
        mismatch["reason"] = (
            f"profile drift: {label}={_display(actual)} "
            f"(expected {_display(expected)})"
        )
        mismatches.append(mismatch)

    reason_code = None
    if mismatches:
        reason_code = (
            "runtime_profile_invalid"
            if any(item["key"] in {"profile_version", "profile_hash"}
                   for item in mismatches)
            else "runtime_profile_drift"
        )
    return {
        "eligible": not mismatches,
        "reason_code": reason_code,
        "advertised_hash": advertised_hash or None,
        "mismatches": mismatches,
    }


__all__ = [
    "EXPECTED_WORK_MODULES",
    "RUNTIME_BINARIES",
    "RUNTIME_PROFILE_REQUIREMENT_SCHEMA",
    "RUNTIME_PROFILE_SCHEMA",
    "RUNTIME_PROFILE_VERSION",
    "build_runtime_profile",
    "evaluate_runtime_profile",
    "runtime_env_key",
    "runtime_profile_requirement",
]
