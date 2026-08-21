"""Canonical server-owned execution-assignment contracts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


SCHEMA = "switchboard.execution_assignment.v1"
EXACT_HEAD_ROLES = frozenset({"review_merge", "remediation"})
VALID_ROLES = frozenset({"implementation", *EXACT_HEAD_ROLES})
OFFLINE_EVIDENCE_PROFILE = "offline_evidence"
CODE_STRICT_PROFILE = "code_strict"
SWITCHBOARD_CI_VERIFICATION_PROFILE = "switchboard_ci_locked_v1"
MISSION_LAUNCH_POINTER_SCHEMA = "switchboard.mission_launch_pointer.v4"
REVIEW_REMEDIATION_POINTER_SCHEMA = (
    "switchboard.review_remediation_launch_pointer.v4"
)
CODEX_CONTEXT_PROFILE_SCHEMA = "switchboard.codex_context_profile.v1"

DEFAULT_TYPED_TOOLS: dict[str, str] = {
    "executed_test_run": "record_executed_test_run",
    "agent_requires_human": "agent_requires_human",
    "stale_assignment": "report_stale_assignment",
}
MISSION_TYPED_TOOLS: dict[str, str] = {
    "executed_test_run": "record_executed_test_run",
    "agent_requires_human": "agent_requires_human",
    "mission_context": "get_mission_context",
    "mission_yield": "yield_mission",
}
MISSION_KEY_PREFIXES: tuple[str, ...] = ("v4:", "v5:")

# These are names, not free-form Codex command fragments.  The same catalog is
# imported by the host launcher, so Coordination and Capacity cannot silently
# disagree about what a selected profile means.
CODEX_CONTEXT_PROFILES: dict[str, dict[str, Any]] = {
    "luna-max-large-codebase": {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "context_window": 600000,
        "auto_compact_token_limit": 500000,
    },
    "luna-max-long-running": {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "context_window": 800000,
        "auto_compact_token_limit": 720000,
    },
}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MISSION_POINTER_FIELDS = frozenset({
    "schema",
    "event_id",
    "event_sequence",
    "ci_context",
    "failure_state",
    "evidence_url",
    "exact_head_sha",
})
_REVIEW_REMEDIATION_POINTER_FIELDS = frozenset({
    "schema",
    "event_id",
    "event_sequence",
    "verdict_id",
    "remediation_id",
    "finding_ids",
    "evidence_url",
    "exact_head_sha",
})

#: Every field name this module can put in a contract, including the optional
#: ones. ``require_exact_execution_assignment`` compares whole dicts, so an
#: Agent Host running an older copy of THIS FILE derives a different field set
#: and every launch is refused — the failure only surfaces after the wake is
#: claimed and the 90s hold expires.
#:
#: Adding a field here (BUG-249 added ``session_policy_profile``) is therefore a
#: wire-breaking change. Conditional contract semantics are declared beside
#: this tuple and are also included in the fingerprint below. Hosts report the
#: fingerprint their bundled copy produces, and a host whose fingerprint
#: differs from the server's is not eligible for work.
#:
#: KEEP IN SYNC with build_execution_assignment. The conformance test
#: tests/test_host_contract_fingerprint.py fails if a field is added to the
#: builder without being listed here.
CONTRACT_FIELDS: tuple[str, ...] = (
    "schema",
    "task_id",
    "execution_id",
    "assignment_id",
    "generation",
    "desired_role",
    "exact_head_sha",
    "exact_pr",
    "workspace_assignment",
    "claim_expectations",
    "typed_tools",
    "session_policy_profile",
    "context_profile",
    "verification_profile",
    "launch_pointer",
)


def contract_fingerprint() -> str:
    """Stable id of the contract shape and conditional semantics this build produces.

    Field names alone are insufficient. A server and Host can expose the same
    keys but select different typed tools for a mission prefix; exact launch
    validation then refuses every wake even though the old shape-only digest
    agrees. Keep every conditional contract input in this manifest.
    """
    payload = json.dumps(
        {
            "schema": SCHEMA,
            "fields": sorted(CONTRACT_FIELDS),
            "default_typed_tools": DEFAULT_TYPED_TOOLS,
            "mission_typed_tools": MISSION_TYPED_TOOLS,
            "mission_key_prefixes": sorted(MISSION_KEY_PREFIXES),
            "offline_evidence_profile": OFFLINE_EVIDENCE_PROFILE,
            "code_strict_profile": CODE_STRICT_PROFILE,
            "switchboard_ci_verification_profile": (
                SWITCHBOARD_CI_VERIFICATION_PROFILE
            ),
            "codex_context_profiles": CODEX_CONTEXT_PROFILES,
            "mission_launch_pointer_schemas": sorted({
                MISSION_LAUNCH_POINTER_SCHEMA,
                REVIEW_REMEDIATION_POINTER_SCHEMA,
            }),
        },
        sort_keys=True, separators=(",", ":"),
    )
    return "eac1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def claim_expectations_for(profile: str, role: str) -> dict[str, Any]:
    """The one claim-expectation shape both mint and bind derive from.

    Only the server-stamped ``offline_evidence`` profile relaxes the Work
    Session requirement; an absent or unknown profile keeps the strict code
    contract so pre-profile wakes and tampered contracts fail closed.
    """
    return {
        "required": True,
        "work_session_required": (
            str(profile or "").strip().lower() != OFFLINE_EVIDENCE_PROFILE
        ),
        "role": role,
    }


def verification_profile_for(
    session_policy_profile: str,
    execution_context: Mapping[str, Any] | None,
) -> str:
    """Select the one bounded Capacity proof owned by Coordination.

    This returns a name, never a command.  Capacity owns how that name is
    materialized and proven, while the immutable assignment makes the selected
    policy visible to the runner and claim bind.
    """
    profile = str(session_policy_profile or "").strip().lower()
    repository = str(
        (execution_context or {}).get("repository") or ""
    ).strip().lower()
    if (
        profile == CODE_STRICT_PROFILE
        and repository == "6th-element-labs/projectplanner"
    ):
        return SWITCHBOARD_CI_VERIFICATION_PROFILE
    return ""


class ExecutionAssignmentError(ValueError):
    """A lifecycle cannot produce a safe execution assignment."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def resolve_codex_context_profile(profile: Any) -> tuple[str, dict[str, Any]]:
    """Resolve one allowlisted Codex context profile.

    The wire contract carries only the profile name.  Keeping the effective
    values in this module lets the server, Agent Host, and native worker use the
    same validated tuple without passing provider command text through a wake.
    """
    selected = str(profile or "").strip().lower()
    if not selected:
        return "", {}
    values = CODEX_CONTEXT_PROFILES.get(selected)
    if not isinstance(values, dict):
        raise ExecutionAssignmentError("execution_assignment_context_profile_unknown")
    model = str(values.get("model") or "").strip()
    effort = str(values.get("reasoning_effort") or "").strip().lower()
    try:
        context_window = int(values.get("context_window") or 0)
        auto_compact = int(values.get("auto_compact_token_limit") or 0)
    except (TypeError, ValueError) as exc:
        raise ExecutionAssignmentError(
            "execution_assignment_context_profile_invalid") from exc
    if (
        not model
        or effort != "max"
        or context_window <= 0
        or auto_compact <= 0
        or auto_compact >= context_window
    ):
        raise ExecutionAssignmentError("execution_assignment_context_profile_invalid")
    return selected, {
        "schema": CODEX_CONTEXT_PROFILE_SCHEMA,
        "profile": selected,
        "model": model,
        "reasoning_effort": effort,
        "context_window": context_window,
        "auto_compact_token_limit": auto_compact,
    }


def context_profile_from_lifecycle(lifecycle: Mapping[str, Any]) -> str:
    """Return the canonical profile name from a lifecycle request.

    A few launch surfaces existed before this field was named.  Accepting their
    read-only aliases at the boundary keeps adapters interoperable while the
    server emits only ``context_profile`` in the immutable assignment.
    """
    supplied = [
        str(lifecycle.get(key) or "").strip().lower()
        for key in (
            "context_profile",
            "profile",
            "codex_profile",
            "launcher_profile",
            "codex_context_profile",
            "model_profile",
            "requested_context_profile",
        )
        if str(lifecycle.get(key) or "").strip()
    ]
    if not supplied:
        return ""
    if len(set(supplied)) != 1:
        raise ExecutionAssignmentError(
            "execution_assignment_context_profile_conflict")
    selected, _ = resolve_codex_context_profile(supplied[0])
    return selected


def normalize_mission_launch_pointer(
    pointer: Mapping[str, Any] | None,
    *,
    expected_head_sha: str,
) -> dict[str, Any]:
    """Validate one bounded durable cause pointer used for v4 remediation.

    This is an immutable evidence address, not a diagnosis.  Keeping an exact
    field set prevents a mission dossier, log excerpt, or prompt from growing
    into a second coordination authority inside the runner assignment.
    """
    if not isinstance(pointer, Mapping) or not pointer:
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_missing")
    schema = str(pointer.get("schema") or "").strip()
    if schema == REVIEW_REMEDIATION_POINTER_SCHEMA:
        return _normalize_review_remediation_pointer(
            pointer, expected_head_sha=expected_head_sha,
        )
    if set(pointer) != _MISSION_POINTER_FIELDS:
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_invalid")

    event_id = str(pointer.get("event_id") or "").strip()
    ci_context = str(pointer.get("ci_context") or "").strip()
    failure_state = str(pointer.get("failure_state") or "").strip().lower()
    evidence_url = str(pointer.get("evidence_url") or "").strip()
    exact_head_sha = str(pointer.get("exact_head_sha") or "").strip().lower()
    expected_head = str(expected_head_sha or "").strip().lower()
    try:
        event_sequence = int(pointer.get("event_sequence") or 0)
    except (TypeError, ValueError) as exc:
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_invalid") from exc

    if (
        schema != MISSION_LAUNCH_POINTER_SCHEMA
        or not event_id
        or len(event_id) > 128
        or event_sequence <= 0
        or not ci_context
        or len(ci_context) > 255
        or not failure_state
        or len(failure_state) > 64
        or not evidence_url.startswith(("https://", "http://"))
        or len(evidence_url) > 2048
        or not _SHA.fullmatch(exact_head_sha)
    ):
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_invalid")
    if not _SHA.fullmatch(expected_head) or exact_head_sha != expected_head:
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_head_mismatch")
    return {
        "schema": schema,
        "event_id": event_id,
        "event_sequence": event_sequence,
        "ci_context": ci_context,
        "failure_state": failure_state,
        "evidence_url": evidence_url,
        "exact_head_sha": exact_head_sha,
    }


def _normalize_review_remediation_pointer(
    pointer: Mapping[str, Any],
    *,
    expected_head_sha: str,
) -> dict[str, Any]:
    """Validate a review verdict/finding address without importing its prose."""
    if set(pointer) != _REVIEW_REMEDIATION_POINTER_FIELDS:
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_invalid")

    event_id = str(pointer.get("event_id") or "").strip()
    verdict_id = str(pointer.get("verdict_id") or "").strip()
    remediation_id = str(pointer.get("remediation_id") or "").strip()
    evidence_url = str(pointer.get("evidence_url") or "").strip()
    exact_head_sha = str(pointer.get("exact_head_sha") or "").strip().lower()
    expected_head = str(expected_head_sha or "").strip().lower()
    raw_finding_ids = pointer.get("finding_ids")
    if not isinstance(raw_finding_ids, list):
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_invalid")
    finding_ids = sorted({
        str(finding_id or "").strip()
        for finding_id in raw_finding_ids
        if str(finding_id or "").strip()
    })
    try:
        event_sequence = int(pointer.get("event_sequence") or 0)
    except (TypeError, ValueError) as exc:
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_invalid") from exc

    if (
        not event_id
        or len(event_id) > 128
        or event_sequence <= 0
        or not verdict_id
        or len(verdict_id) > 128
        or not remediation_id
        or len(remediation_id) > 128
        or not finding_ids
        or len(finding_ids) > 64
        or any(len(finding_id) > 128 for finding_id in finding_ids)
        or not evidence_url.startswith(("https://", "http://"))
        or len(evidence_url) > 2048
        or not _SHA.fullmatch(exact_head_sha)
    ):
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_invalid")
    if not _SHA.fullmatch(expected_head) or exact_head_sha != expected_head:
        raise ExecutionAssignmentError(
            "execution_assignment_remediation_pointer_head_mismatch")
    return {
        "schema": REVIEW_REMEDIATION_POINTER_SCHEMA,
        "event_id": event_id,
        "event_sequence": event_sequence,
        "verdict_id": verdict_id,
        "remediation_id": remediation_id,
        "finding_ids": finding_ids,
        "evidence_url": evidence_url,
        "exact_head_sha": exact_head_sha,
    }


def build_execution_assignment(
    *,
    task_id: str,
    assignment: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    execution_context: Mapping[str, Any] | None = None,
    prior_attempts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive the tiny immutable identity/scope contract for one boot.

    Coordination may use ``prior_attempts`` and a rich mission dossier while
    deciding to dispatch.  Neither belongs in the runner assignment: the agent
    rereads live Switchboard and GitHub facts after boot.  Keeping interpreted
    history out of this contract prevents cached diagnosis from masquerading as
    lifecycle authority.
    """
    del prior_attempts

    role = str(lifecycle.get("role") or "implementation")
    if role not in VALID_ROLES:
        raise ExecutionAssignmentError("execution_assignment_role_invalid")
    head_sha = str(lifecycle.get("head_sha") or "")
    if role in EXACT_HEAD_ROLES and not head_sha:
        raise ExecutionAssignmentError("execution_assignment_exact_head_missing")
    execution_id = str(lifecycle.get("execution_id") or "")
    if not execution_id:
        raise ExecutionAssignmentError("execution_assignment_execution_id_missing")
    generation = int(lifecycle.get("generation") or 0)
    if generation <= 0:
        raise ExecutionAssignmentError("execution_assignment_generation_invalid")
    assignment_id = str(assignment.get("assignment_id") or "")
    if not assignment_id:
        raise ExecutionAssignmentError("execution_assignment_id_missing")

    # The profile is server-stamped into the lifecycle at wake mint. It is
    # omitted (not defaulted) when absent so contracts minted before the
    # profile existed rebuild byte-identically on the claim path.
    profile = str(lifecycle.get("session_policy_profile") or "").strip().lower()
    typed_tools = dict(DEFAULT_TYPED_TOOLS)
    # A v4 mission is already inside the durable pager contract.  Returning a
    # stale observation through the legacy completion-run factory would create
    # a second lifecycle owner and can race the reporting runner.  Its exact
    # handoff is the journal-backed yield command instead: Coordination records
    # the new cursor/role and Capacity independently acknowledges surrender.
    if str(lifecycle.get("mission_key") or "").strip().startswith(
        MISSION_KEY_PREFIXES
    ):
        typed_tools = dict(MISSION_TYPED_TOOLS)

    context = dict(execution_context or {})
    contract: dict[str, Any] = {
        "schema": SCHEMA,
        "task_id": str(task_id or "").strip().upper(),
        "execution_id": execution_id,
        "assignment_id": assignment_id,
        "generation": generation,
        "desired_role": role,
        "exact_head_sha": head_sha,
        "exact_pr": {
            "number": int(lifecycle.get("pr_number") or 0),
            "url": str(lifecycle.get("pr_url") or ""),
        },
        "workspace_assignment": {
            "repository": str(context.get("repository") or ""),
            "base_sha": str(context.get("base_sha") or ""),
            "checkout_sha": str(context.get("checkout_sha") or ""),
            "checkout_requirements": dict(
                context.get("checkout_requirements") or {}),
            "context_digest": str(context.get("digest") or ""),
        },
        "claim_expectations": claim_expectations_for(profile, role),
        # Sibling of claim_expectations (not nested): the Connect claim bind
        # hard-compares claim_expectations to the exact shape derived by
        # claim_expectations_for. Typed tool names live here so agents see
        # them without breaking that bind.
        "typed_tools": typed_tools,
    }
    if profile:
        contract["session_policy_profile"] = profile
    context_profile = context_profile_from_lifecycle(lifecycle)
    if context_profile:
        runtime = str(
            (context.get("runtime") or {}).get("registry_name")
            or assignment.get("runtime") or ""
        ).strip().lower()
        if runtime and runtime not in {"codex", "openai"}:
            raise ExecutionAssignmentError(
                "execution_assignment_context_profile_runtime_invalid")
        contract["context_profile"] = context_profile
    verification_profile = str(
        lifecycle.get("verification_profile") or ""
    ).strip().lower()
    if verification_profile:
        contract["verification_profile"] = verification_profile
    # A launch pointer is intentionally not a diagnosis. Mission Bot v4 may
    # preserve one exact durable CI event for remediation; all other starts
    # retain the small generic pointer.
    mission_pointer = lifecycle.get("mission_launch_pointer")
    requires_mission_pointer = (
        role == "remediation"
        and str(lifecycle.get("mission_key") or "").strip().startswith("v4:")
    )
    if mission_pointer or requires_mission_pointer:
        pointer = normalize_mission_launch_pointer(
            mission_pointer if isinstance(mission_pointer, Mapping) else None,
            expected_head_sha=head_sha,
        )
    else:
        pointer = {
            "trigger": str(lifecycle.get("reason_code") or ""),
            "evidence_url": _launch_pointer_url(lifecycle),
        }
    contract["launch_pointer"] = pointer
    return contract


def _launch_pointer_url(lifecycle: Mapping[str, Any]) -> str:
    findings = lifecycle.get("acceptance_findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            for key in ("failing_check_url", "evidence_url", "review_url", "url"):
                value = str(finding.get(key) or "").strip()
                if value:
                    return value
    dossier = lifecycle.get("mission_dossier")
    if isinstance(dossier, Mapping):
        for key in ("failing_check_url", "evidence_url", "pr_url"):
            value = str(dossier.get(key) or "").strip()
            if value:
                return value
    return str(lifecycle.get("pr_url") or "").strip()


def require_exact_execution_assignment(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    """Reject any missing, changed, or extra field in the admitted contract."""

    if dict(observed) != dict(expected):
        raise ExecutionAssignmentError("execution_assignment_contract_mismatch")
