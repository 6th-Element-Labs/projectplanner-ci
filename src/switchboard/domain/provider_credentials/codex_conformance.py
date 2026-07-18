"""Fail-closed CO-16 two-row Codex execution conformance evaluation.

This module never launches a provider process and never accepts raw credentials.  It
validates the redacted receipts produced by the personal and direct-API execution
paths so task completion cannot be inferred from host connectivity or a simulated
CLI response.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any

from .ownership import forbidden_public_secret_paths


CODEX_CONFORMANCE_SCHEMA = "switchboard.codex_execution_conformance.v1"
CODEX_CONFORMANCE_ROW_SCHEMA = "switchboard.codex_execution_conformance.row.v1"

_BINDING_FIELDS = (
    "task_id", "claim_id", "work_session_id", "runner_session_id",
    "host_id", "wake_id", "source_sha", "execution_connection_id",
)
_SURFACES = ("ui", "scheduler", "runner", "audit", "capacity", "error")
_PERSONAL_SUBSTRATES = frozenset({"ephemeral", "persistent"})
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _finding(code: str, row: str = "", detail: str = "") -> dict[str, str]:
    result = {"code": code}
    if row:
        result["row"] = row
    if detail:
        result["detail"] = detail
    return result


def _binding_ok(receipt: Mapping[str, Any], connection_id: str,
                source_sha: str) -> bool:
    binding = _mapping(receipt.get("binding"))
    return all(_text(binding.get(field)) for field in _BINDING_FIELDS) \
        and _text(binding.get("task_id")) == "CO-16" \
        and _text(binding.get("source_sha")) == source_sha \
        and _text(binding.get("execution_connection_id")) == connection_id


def _execution_ok(receipt: Mapping[str, Any], connection_id: str,
                  source_sha: str) -> bool:
    return (
        receipt.get("native_cli") is True
        and bool(_text(receipt.get("cli_version")))
        and receipt.get("mcp_registered") is True
        and receipt.get("scoped_read") is True
        and receipt.get("scoped_action") is True
        and receipt.get("cross_scope_denied") is True
        and receipt.get("residue_purged") is True
        and receipt.get("post_revoke_denied") is True
        and _binding_ok(receipt, connection_id, source_sha)
    )


def _surface_findings(row_name: str, row: Mapping[str, Any],
                      connection_id: str) -> list[dict[str, str]]:
    surfaces = _mapping(row.get("surface_receipts"))
    findings: list[dict[str, str]] = []
    for surface in _SURFACES:
        receipt = _mapping(surfaces.get(surface))
        if _text(receipt.get("execution_connection_id")) != connection_id:
            findings.append(_finding(
                "execution_connection_surface_mismatch", row_name, surface))
    return findings


def evaluate_codex_conformance(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a redacted, deterministic CO-16 verdict and typed findings."""
    payload = _mapping(value)
    findings: list[dict[str, str]] = []
    secret_paths = forbidden_public_secret_paths(payload)
    if secret_paths:
        findings.append(_finding(
            "secret_shaped_evidence_denied", detail=",".join(secret_paths)))

    raw_rows = payload.get("rows")
    supplied_rows = _rows(raw_rows)
    if (not isinstance(raw_rows, Sequence)
            or isinstance(raw_rows, (str, bytes, bytearray))
            or len(raw_rows) != len(supplied_rows)):
        findings.append(_finding("conformance_rows_malformed"))
    source_sha = _text(payload.get("source_sha")).lower()
    if not _SOURCE_SHA.fullmatch(source_sha):
        findings.append(_finding("source_sha_invalid"))
    supplied_kinds = [_text(row.get("connection_kind")) for row in supplied_rows]
    if len(supplied_rows) != 2:
        findings.append(_finding("conformance_row_count_invalid"))
    if len(supplied_kinds) != len(set(supplied_kinds)):
        findings.append(_finding("conformance_row_kind_duplicate"))
    if any(kind not in {"personal_subscription", "direct_api"}
           for kind in supplied_kinds):
        findings.append(_finding("conformance_row_kind_unknown"))
    by_kind = {
        _text(row.get("connection_kind")): row
        for row in supplied_rows
        if _text(row.get("connection_kind"))
    }
    expected = {
        "personal_subscription": "chatgpt_subscription",
        "direct_api": "openai_platform_api",
    }
    normalized: list[dict[str, Any]] = []
    connection_ids: list[str] = []

    for kind, billing_mode in expected.items():
        row = by_kind.get(kind) or {}
        connection_id = _text(row.get("execution_connection_id"))
        row_name = "personal" if kind == "personal_subscription" else "api"
        if not row:
            findings.append(_finding("conformance_row_missing", row_name))
            continue
        if _text(row.get("provider")) != "openai-codex":
            findings.append(_finding("provider_mismatch", row_name))
        if not connection_id:
            findings.append(_finding("execution_connection_id_missing", row_name))
        else:
            connection_ids.append(connection_id)
        if _text(row.get("billing_mode")) != billing_mode:
            findings.append(_finding("billing_mode_mismatch", row_name))

        executions = _rows(row.get("executions"))
        if not executions or any(
                not _execution_ok(item, connection_id, source_sha)
                for item in executions):
            findings.append(_finding("native_execution_proof_incomplete", row_name))
        findings.extend(_surface_findings(row_name, row, connection_id))

        if kind == "personal_subscription":
            substrates = {
                _text(item.get("host_class")) for item in executions
                if _text(item.get("host_class"))
            }
            if not _PERSONAL_SUBSTRATES.issubset(substrates):
                findings.append(_finding("personal_substrate_matrix_incomplete", row_name))
            if (row.get("metered") is not False
                    or row.get("api_key_fallback") is not False
                    or _mapping(row.get("cost_receipt"))):
                findings.append(_finding("personal_api_billing_forbidden", row_name))
        else:
            cost = _mapping(row.get("cost_receipt"))
            raw_cost = cost.get("cost_usd")
            try:
                cost_usd = float(raw_cost) if not isinstance(raw_cost, bool) else 0.0
            except (TypeError, ValueError):
                cost_usd = 0.0
            if (not math.isfinite(cost_usd)
                    or cost_usd <= 0
                    or _text(cost.get("execution_connection_id")) != connection_id
                    or not _text(cost.get("billing_account_fingerprint"))
                    or not _text(cost.get("budget_id"))):
                findings.append(_finding("api_cost_receipt_incomplete", row_name))
            if row.get("metered") is not True:
                findings.append(_finding("api_metered_state_missing", row_name))

        normalized.append({
            "schema": CODEX_CONFORMANCE_ROW_SCHEMA,
            "name": row_name,
            "provider": _text(row.get("provider")),
            "connection_kind": kind,
            "execution_connection_id": connection_id,
            "billing_mode": _text(row.get("billing_mode")),
            "execution_count": len(executions),
            "host_classes": sorted({
                _text(item.get("host_class")) for item in executions
                if _text(item.get("host_class"))
            }),
        })

    if len(connection_ids) == 2 and len(set(connection_ids)) != 2:
        findings.append(_finding("execution_connections_not_distinct"))
    negative = _mapping(payload.get("negative_proofs"))
    for name in (
        "personal_failure_did_not_activate_api",
        "api_failure_did_not_activate_personal",
        "cross_user_denied",
        "cross_project_denied",
    ):
        if negative.get(name) is not True:
            findings.append(_finding("negative_proof_missing", detail=name))

    return {
        "schema": CODEX_CONFORMANCE_SCHEMA,
        "ok": not findings,
        "provider": "openai-codex",
        "rows": normalized,
        "negative_proofs": {
            name: negative.get(name) is True for name in (
                "personal_failure_did_not_activate_api",
                "api_failure_did_not_activate_personal",
                "cross_user_denied",
                "cross_project_denied",
            )
        },
        "finding_count": len(findings),
        "findings": findings,
        "evidence_redacted": not secret_paths,
    }


__all__ = [
    "CODEX_CONFORMANCE_ROW_SCHEMA",
    "CODEX_CONFORMANCE_SCHEMA",
    "evaluate_codex_conformance",
]
