#!/usr/bin/env python3
"""Shared, fail-closed contract helpers for vendor-hosted cloud execution.

The module deliberately does not call a vendor.  Per-vendor adapters implement the
transport while these helpers enforce the common Switchboard trigger, adoption,
receipt, concurrency, and status rules defined by ADAPTER-17.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Protocol


SCHEMA = "switchboard.cloud_execution_adapter.v1"
CANONICAL_REPO = "6th-Element-Labs/projectplanner"
REQUIRED_VENDORS = {"claude-code-cloud", "openai-codex-cloud", "cursor-background-agent"}
TRIGGER_SUPPORT = {"conditional", "unsupported"}
TRIGGER_MODES = {"cli_bridge", "http_api", "no_public_trigger"}
DEV_STATUSES = {"queued", "running", "pr", "failed"}
TERMINAL_PROVIDER_STATES = {"completed", "failed", "cancelled", "expired", "lost"}
FORBIDDEN_BRANCHES = {"main", "master"}
DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "cloud_execution_adapter.v1.json"
)


class CloudExecutionAdapter(Protocol):
    """Transport interface implemented by Claude, Codex, and Cursor adapters."""

    vendor_id: str

    def preflight(self, dispatch: dict[str, Any]) -> dict[str, Any]: ...

    def trigger(self, dispatch: dict[str, Any]) -> dict[str, Any]: ...

    def get_session(self, provider_session_id: str) -> dict[str, Any]: ...


def load_contract(path: str | Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    contract = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_contract(contract)
    if errors:
        raise ValueError("invalid cloud execution contract: " + "; ".join(errors))
    return contract


def validate_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(contract, dict):
        return ["contract must be an object"]
    if contract.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    if contract.get("canonical_repo") != CANONICAL_REPO:
        errors.append(f"canonical_repo must be {CANONICAL_REPO}")
    if set(contract.get("dev_statuses") or []) != DEV_STATUSES:
        errors.append("dev_statuses vocabulary is incomplete")
    if not contract.get("security_invariants"):
        errors.append("security_invariants must not be empty")

    vendors = contract.get("vendors") or []
    ids = [str(vendor.get("id") or "") for vendor in vendors]
    if set(ids) != REQUIRED_VENDORS or len(ids) != len(set(ids)):
        errors.append("vendors must contain each required vendor exactly once")
    for vendor in vendors:
        vendor_id = str(vendor.get("id") or "")
        support = vendor.get("trigger_support")
        mode = vendor.get("trigger_mode")
        if support not in TRIGGER_SUPPORT:
            errors.append(f"{vendor_id}: invalid trigger_support")
        if mode not in TRIGGER_MODES:
            errors.append(f"{vendor_id}: invalid trigger_mode")
        if support == "conditional" and not vendor.get("requirements"):
            errors.append(f"{vendor_id}: conditional trigger needs requirements")
        if support == "unsupported" and not vendor.get("unsupported_reason"):
            errors.append(f"{vendor_id}: unsupported trigger needs a reason")
        if int((vendor.get("concurrency") or {}).get("switchboard_default_cap") or 0) < 1:
            errors.append(f"{vendor_id}: concurrency cap must be positive")
        if not (vendor.get("session") or {}).get("id_field"):
            errors.append(f"{vendor_id}: session id_field is required")
        if not (vendor.get("session") or {}).get("url_field"):
            errors.append(f"{vendor_id}: session url_field is required")
        if not vendor.get("status_map"):
            errors.append(f"{vendor_id}: status_map is required")
        if not vendor.get("billing"):
            errors.append(f"{vendor_id}: billing contract is required")
    return errors


def _vendor(contract: dict[str, Any], vendor_id: str) -> dict[str, Any] | None:
    return next((vendor for vendor in contract["vendors"] if vendor["id"] == vendor_id), None)


def validate_dispatch_envelope(dispatch: dict[str, Any]) -> list[str]:
    """Validate provider-neutral launch input without accepting raw credentials."""
    errors: list[str] = []
    if not isinstance(dispatch, dict):
        return ["dispatch envelope must be an object"]
    required = ("project", "task_id", "wake_id", "dev_brief", "canonical_repo", "branch")
    for field in required:
        if not dispatch.get(field):
            errors.append(f"{field} is required")
    if dispatch.get("project") != "switchboard":
        errors.append("project must be switchboard")
    if dispatch.get("canonical_repo") != CANONICAL_REPO:
        errors.append("canonical_repo is not the configured code-truth repo")
    branch = str(dispatch.get("branch") or "")
    if branch in FORBIDDEN_BRANCHES or "/" not in branch:
        errors.append("branch must be a task branch and never main/master")
    mcp_access = dispatch.get("mcp_access") or {}
    if not mcp_access.get("endpoint") or not mcp_access.get("token_ref"):
        errors.append("mcp_access requires endpoint and opaque token_ref")
    if mcp_access.get("token") or dispatch.get("mcp_token"):
        errors.append("raw MCP tokens are forbidden in dispatch envelopes")
    scopes = set(mcp_access.get("scopes") or [])
    if not {"read:task", "write:claim", "write:evidence"} <= scopes:
        errors.append("mcp_access scopes are incomplete")
    if not mcp_access.get("expires_at"):
        errors.append("mcp_access requires an expiry")
    return errors


def evaluate_trigger(
    vendor_id: str,
    dispatch: dict[str, Any],
    available_requirements: Iterable[str],
    active_sessions: int,
    *,
    provider_result: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a vendor launch and adopt it only with a complete provider receipt."""
    contract = contract or load_contract()
    vendor = _vendor(contract, vendor_id)
    if vendor is None:
        return _deny("unknown_vendor", "invalid_input")
    envelope_errors = validate_dispatch_envelope(dispatch)
    if envelope_errors:
        return _deny("invalid_dispatch_envelope", "invalid_input", errors=envelope_errors)
    if vendor["trigger_support"] == "unsupported":
        return _deny(
            "provider_trigger_unsupported",
            "missing_data",
            vendor_id=vendor_id,
            detail=vendor["unsupported_reason"],
        )

    missing = sorted(set(vendor["requirements"]) - set(available_requirements))
    if missing:
        return _deny("missing_provider_setup", "absent_permission", vendor_id=vendor_id,
                     missing=missing)
    cap = int(vendor["concurrency"]["switchboard_default_cap"])
    try:
        active_session_count = int(active_sessions)
    except (TypeError, ValueError):
        return _deny("active_session_count_invalid", "invalid_input", vendor_id=vendor_id)
    if active_session_count < 0:
        return _deny("active_session_count_invalid", "invalid_input", vendor_id=vendor_id)
    if active_session_count >= cap:
        return _deny("provider_concurrency_cap_reached", "failed_gate", vendor_id=vendor_id,
                     active_sessions=active_session_count, cap=cap)
    if provider_result is None:
        return {
            "allowed": True,
            "adopted": False,
            "vendor_id": vendor_id,
            "dev_status": "queued",
            "reason": "provider_trigger_ready",
        }
    if not isinstance(provider_result, dict):
        return _deny("provider_response_malformed", "malformed_payload", vendor_id=vendor_id)
    if provider_result.get("error") or provider_result.get("ok") is False:
        return _deny("vendor_api_error", "broken_connection", vendor_id=vendor_id,
                     provider_error=provider_result.get("error") or "provider rejected trigger")

    session_contract = vendor["session"]
    session_id = provider_result.get(session_contract["id_field"])
    session_url = provider_result.get(session_contract["url_field"])
    if not session_id or not session_url:
        return _deny("adoption_receipt_incomplete", "missing_data", vendor_id=vendor_id,
                     missing=[name for name, value in (
                         (session_contract["id_field"], session_id),
                         (session_contract["url_field"], session_url),
                     ) if not value])
    provider_state = str(provider_result.get("status") or "running").lower()
    if provider_state in {"expired", "lost"}:
        return _deny(f"vendor_session_{provider_state}", "unreachable_agent",
                     vendor_id=vendor_id, provider_session_id=session_id)
    return {
        "allowed": True,
        "adopted": True,
        "vendor_id": vendor_id,
        "provider_session_id": str(session_id),
        "session_url": str(session_url),
        "runner_session_id": f"cloud/{vendor_id}/{session_id}",
        "wake_id": dispatch["wake_id"],
        "task_id": dispatch["task_id"],
        "claim_id": dispatch.get("claim_id"),
        "dev_status": "running",
        "provider_status": provider_state,
        "receipt_schema": "switchboard.cloud_session_binding.v1",
    }


def refresh_session(
    vendor_id: str,
    provider_result: dict[str, Any],
    *,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map provider readback to Switchboard state, making lost sessions visibly fail."""
    contract = contract or load_contract()
    vendor = _vendor(contract, vendor_id)
    if vendor is None:
        return _deny("unknown_vendor", "invalid_input")
    if not isinstance(provider_result, dict):
        return _deny("provider_response_malformed", "malformed_payload", vendor_id=vendor_id)
    state = str(provider_result.get("status") or "").lower()
    if not state:
        return _deny("provider_status_missing", "missing_data", vendor_id=vendor_id)
    mapped = (vendor.get("status_map") or {}).get(state)
    if not mapped:
        return _deny("provider_status_unknown", "invalid_input", vendor_id=vendor_id,
                     provider_status=state)
    if state in {"expired", "lost"}:
        return _deny(f"vendor_session_{state}", "unreachable_agent", vendor_id=vendor_id,
                     dev_status="failed")
    return {"allowed": True, "vendor_id": vendor_id, "provider_status": state,
            "dev_status": mapped, "terminal": state in TERMINAL_PROVIDER_STATES}


def project_dev_status(*, wake_status: str = "", session_active: bool = False,
                       pr_url: str = "", failed: bool = False) -> str:
    if pr_url:
        return "pr"
    if failed:
        return "failed"
    if session_active:
        return "running"
    if wake_status in {"pending", "requested", "claimed"}:
        return "queued"
    return "failed"


def validate_usage_receipt(receipt: dict[str, Any]) -> list[str]:
    """Keep subscription and provider-usage billing honest in Tally."""
    errors: list[str] = []
    if not isinstance(receipt, dict):
        return ["usage receipt must be an object"]
    if receipt.get("source") not in {"agent_report", "provider_reconcile"}:
        errors.append("source must be agent_report or provider_reconcile")
    if receipt.get("confidence") not in {"exact", "reported", "estimated", "unknown"}:
        errors.append("invalid confidence")
    billing_mode = receipt.get("billing_mode")
    if billing_mode not in {"api_usage", "subscription", "unknown"}:
        errors.append("invalid billing_mode")
    if billing_mode == "subscription" and receipt.get("confidence") == "exact":
        errors.append("subscription allocation cannot claim exact confidence")
    if billing_mode == "subscription" and float(receipt.get("cost_usd") or 0) != 0:
        errors.append("subscription cost must remain zero until provider reconciliation")
    if not receipt.get("task_id") or not receipt.get("vendor_id"):
        errors.append("task_id and vendor_id are required")
    return errors


def _deny(reason: str, failure_class: str, **detail: Any) -> dict[str, Any]:
    return {"allowed": False, "adopted": False, "dev_status": "failed",
            "reason": reason, "failure_class": failure_class, **detail}
