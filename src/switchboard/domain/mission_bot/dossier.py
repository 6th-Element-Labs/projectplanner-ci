"""Package live facts for durable identity and a bounded Mission Bot tape.

The complete dossier remains available to decision history.  A runner receives
only actionable fields plus a durable evidence reference, so provider argv
limits cannot turn a valid mission into a Capacity launch failure.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping, Sequence

from switchboard.domain.mission_bot.facts import finding_detail
from switchboard.domain.mission_bot.outputs import MISSION_DOSSIER_SCHEMA


_FAILED = {
    "failure", "failed", "error", "timed_out", "action_required",
    "cancelled", "canceled", "stale", "startup_failure",
}

#: Keys never copied into the LLM prompt (env, tokens, credentials).
_PROMPT_REDACT_KEYS = frozenset({
    "env",
    "env_json",
    "session_token",
    "session_token_hash",
    "token",
    "tokens",
    "api_key",
    "api_keys",
    "secret",
    "secrets",
    "password",
    "passwd",
    "credentials",
    "credential",
    "authorization",
    "auth_header",
    "gh_token",
    "github_token",
    "private_key",
    "mcp_token",
    "relay_ticket",
    "ticket",
    "bearer",
    "access_token",
    "refresh_token",
})

# Observation clocks belong in the dossier presented to the agent, but they do
# not identify a new mission.  Hashing them made an unchanged red head mint a
# fresh remediation effect every coordinator tick (BUG-207).
_VOLATILE_IDENTITY_KEYS = frozenset({
    "age_seconds",
    "checked_at",
    "created_at",
    "evidence_age_seconds",
    "expires_at",
    "generated_at",
    "hydration_started_at",
    "last_heartbeat_at",
    "observed_at",
    "source_observed_at",
    "updated_at",
    "uptime_seconds",
    "waited_seconds",
})

MISSION_TAPE_MAX_BYTES = 32 * 1024
MISSION_TAPE_MAX_ITEMS = 16
MISSION_TAPE_MAX_TEXT = 1024


def _map(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _text_raw(value: Any) -> str:
    return str(value or "").strip()


def _deepcopy_rows(value: Any) -> list[Any]:
    if isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        return []
    if not isinstance(value, Sequence):
        return []
    return [copy.deepcopy(item) for item in value]


def _finding_rows(findings: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _deepcopy_rows(findings):
        if isinstance(item, Mapping) and item.get("blocking") is not False:
            rows.append(dict(item))
    return rows


def _failing_checks(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every failing required context, unchanged — not a single representative."""
    required = list(snapshot.get("required_status_contexts") or [])
    contexts = _map(snapshot.get("status_contexts"))
    failing: list[dict[str, Any]] = []
    for name in required:
        row = _map(contexts.get(name))
        state = _text(
            row.get("conclusion") or row.get("state") or row.get("status")
        ).lower().replace("-", "_").replace(" ", "_")
        if not row or state not in _FAILED:
            continue
        entry = dict(row)
        entry.setdefault("context", _text_raw(name))
        entry.setdefault("name", _text_raw(name))
        failing.append(entry)
    failing.sort(key=lambda item: str(item.get("context") or item.get("name") or ""))
    return failing


def _check_url(row: Mapping[str, Any]) -> str:
    return _text_raw(
        row.get("target_url") or row.get("url")
        or row.get("details_url") or row.get("detailsUrl")
        or row.get("run_url")
    )


def _check_summary(row: Mapping[str, Any]) -> str:
    return _text_raw(
        row.get("description") or row.get("summary") or row.get("output_title")
    )


def _run_attempt(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("run_attempt") or row.get("runAttempt") or 0)
    except (TypeError, ValueError):
        return 0


def _stable_digest(value: Any) -> str:
    """Compact digest so material nested evidence changes mint new keys."""
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _stable_evidence_value(value: Any) -> Any:
    """Remove observation churn while preserving the complete agent dossier."""
    if isinstance(value, Mapping):
        code = str(value.get("code") or "")
        return {
            str(key): _stable_evidence_value(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_IDENTITY_KEYS
            # This server-generated sentence embeds the elapsed minute.  Its
            # durable identity is the finding code plus exact head below.
            and not (
                str(key) == "message"
                and code == "review_stalled_no_verdict"
            )
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_stable_evidence_value(item) for item in value]
    return value


def evidence_identity(dossier: Mapping[str, Any]) -> dict[str, Any]:
    """Stable evidence keys that must enter mission idempotency."""
    failing = list(dossier.get("failing_checks") or [])
    findings = list(dossier.get("acceptance_findings") or [])
    review = _map(dossier.get("review"))
    queue = _map(dossier.get("merge_queue"))
    return {
        "failing_contexts": [
            str(item.get("context") or item.get("name") or "")
            for item in failing
            if isinstance(item, Mapping)
        ],
        "failing_check_urls": sorted({
            _check_url(item) for item in failing
            if isinstance(item, Mapping) and _check_url(item)
        }),
        "failing_run_attempts": sorted({
            _run_attempt(item) for item in failing
            if isinstance(item, Mapping) and _run_attempt(item) > 0
        }),
        "finding_codes": [
            str(item.get("code") or "")
            for item in findings
            if isinstance(item, Mapping) and item.get("code")
        ],
        # Material nested surfaces — same codes with different details must
        # not replay an obsolete boot.
        "findings_digest": _stable_digest(_stable_evidence_value(findings)),
        "failing_checks_digest": _stable_digest(
            _stable_evidence_value(failing)
        ),
        "review_digest": _stable_digest(_stable_evidence_value({
            "status": review.get("status") or review.get("state")
            or review.get("verdict"),
            "head_sha": review.get("head_sha"),
            "findings": review.get("findings") or [],
            "summary": review.get("summary") or review.get("body") or "",
            "details": review.get("details") or review.get("review_details") or {},
        })),
        "queue_digest": _stable_digest(_stable_evidence_value({
            "state": queue.get("state") or queue.get("status"),
            "last_removal_reason": (
                queue.get("last_removal_reason") or queue.get("removal_reason")
            ),
            "failure_evidence": (
                queue.get("failure_evidence") or queue.get("evidence") or {}
            ),
            "messages": queue.get("messages") or queue.get("message") or "",
            "retry_exhausted": queue.get("retry_exhausted"),
            "prior_enqueue_verified": queue.get("prior_enqueue_verified"),
        })),
        "missing_artifact_expected_key": str(
            _map(dossier.get("missing_artifact")).get("expected_key") or ""
        ),
        "missing_artifact_digest": _stable_digest(
            _stable_evidence_value(dossier.get("missing_artifact") or {})
        ),
    }


def _redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "<redacted:depth>"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in _PROMPT_REDACT_KEYS:
                out[name] = "<redacted>"
                continue
            out[name] = _redact_value(item, depth=depth + 1)
        return out
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, Sequence):
        return [_redact_value(item, depth=depth + 1) for item in value]
    return value


def _bounded_text(value: Any, limit: int = MISSION_TAPE_MAX_TEXT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"<truncated:{len(text) - limit}>"


def _bounded_rows(value: Any, *, keys: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source = _deepcopy_rows(value)
    for item in source[:MISSION_TAPE_MAX_ITEMS]:
        if not isinstance(item, Mapping):
            continue
        row: dict[str, Any] = {}
        for key in keys:
            current = item.get(key)
            if current in (None, "", [], {}):
                continue
            if isinstance(current, Sequence) and not isinstance(
                    current, (str, bytes)):
                row[key] = [
                    _bounded_text(part, 256)
                    for part in list(current)[:MISSION_TAPE_MAX_ITEMS]
                ]
            elif isinstance(current, Mapping):
                row[key] = {
                    str(name): _bounded_text(part, 512)
                    for name, part in list(current.items())[:MISSION_TAPE_MAX_ITEMS]
                    if part not in (None, "", [], {})
                }
            else:
                row[key] = _bounded_text(current)
        rows.append(row)
    return rows


def _bounded_mapping(value: Any, *, keys: Sequence[str]) -> dict[str, Any]:
    source = _map(value)
    return {
        key: _bounded_text(source.get(key))
        for key in keys
        if source.get(key) not in (None, "", [], {})
    }


def _mission_tape(dossier: Mapping[str, Any]) -> dict[str, Any]:
    """Project a complete dossier into an explicit, inspectable launch budget."""
    source = _redact_value(copy.deepcopy(dict(dossier)))
    task = _map(source.get("task"))
    github_pr = _map(source.get("github_pr"))
    required = [
        _bounded_text(item, 256)
        for item in list(source.get("required_status_contexts") or [])[
            :MISSION_TAPE_MAX_ITEMS
        ]
    ]
    status_contexts = _map(source.get("status_contexts"))
    checks = _bounded_rows(
        source.get("failing_checks"),
        keys=(
            "context", "name", "state", "status", "conclusion", "target_url",
            "url", "details_url", "run_url", "description", "summary",
            "output_title", "run_attempt",
        ),
    )
    findings = _bounded_rows(
        source.get("acceptance_findings"),
        keys=(
            "code", "message", "blocking", "failure_class", "finding_class",
            "severity", "affected_surface", "observed_behavior",
            "expected_behavior", "repro_steps", "failing_contexts",
            "failing_check_url", "failing_check_summary", "failing_run_attempt",
            "missing_artifact",
        ),
    )
    tape: dict[str, Any] = {
        "schema": source.get("schema") or MISSION_DOSSIER_SCHEMA,
        "mission": _bounded_text(source.get("mission")),
        "reason_code": _bounded_text(source.get("reason_code"), 256),
        "task_id": _bounded_text(source.get("task_id"), 128),
        "pr_number": int(source.get("pr_number") or 0),
        "pr_url": _bounded_text(source.get("pr_url"), 1024),
        "head_sha": _bounded_text(source.get("head_sha"), 128),
        "board_status": _bounded_text(source.get("board_status"), 128),
        "task": _bounded_mapping(
            task,
            keys=(
                "title", "description", "entry_criteria", "exit_criteria",
                "status", "workstream", "risk_level",
            ),
        ),
        "github_pr": _bounded_mapping(
            github_pr,
            keys=(
                "title", "body", "url", "html_url", "state", "is_draft",
                "mergeable", "merge_state_status", "head_sha", "base_ref",
                "head_ref",
            ),
        ),
        "required_status_contexts": required,
        "status_contexts": {
            name: _bounded_mapping(
                status_contexts.get(name),
                keys=(
                    "state", "status", "conclusion", "target_url", "url",
                    "details_url", "description", "summary", "run_attempt",
                ),
            )
            for name in required
            if isinstance(status_contexts.get(name), Mapping)
        },
        "acceptance_findings": findings,
        "failing_checks": checks,
        "failing_contexts": [
            _bounded_text(item, 256)
            for item in list(source.get("failing_contexts") or [])[
                :MISSION_TAPE_MAX_ITEMS
            ]
        ],
        "failing_check_url": _bounded_text(
            source.get("failing_check_url"), 1024
        ),
        "failing_check_summary": _bounded_text(
            source.get("failing_check_summary")
        ),
        "failing_run_attempt": int(source.get("failing_run_attempt") or 0),
        "review": _bounded_mapping(
            source.get("review"),
            keys=("status", "state", "verdict", "head_sha", "summary", "body"),
        ),
        "merge_gate": _bounded_mapping(
            source.get("merge_gate"),
            keys=("status", "state", "message", "reason", "allowed"),
        ),
        "merge_queue": _bounded_mapping(
            source.get("merge_queue"),
            keys=(
                "status", "state", "url", "message", "messages",
                "last_removal_reason", "removal_reason",
            ),
        ),
        "dependency_state": _bounded_mapping(
            source.get("dependency_state"),
            keys=("ready", "satisfied", "blocked_by_count", "message"),
        ),
        "agent_blocker": _bounded_mapping(
            source.get("agent_blocker"),
            keys=("reason", "question", "evidence", "created_by"),
        ),
        "missing_artifact": _bounded_mapping(
            source.get("missing_artifact"),
            keys=("expected_key", "message", "reason", "repair"),
        ),
        "evidence_identity": {
            key: (
                [
                    _bounded_text(item, 512)
                    for item in list(value)[:MISSION_TAPE_MAX_ITEMS]
                ]
                if isinstance(value, Sequence)
                and not isinstance(value, (str, bytes))
                else _bounded_text(value, 512)
            )
            for key, value in _map(source.get("evidence_identity")).items()
            if key in {
                "failing_contexts", "failing_check_urls",
                "failing_run_attempts", "finding_codes", "findings_digest",
                "failing_checks_digest", "review_digest", "queue_digest",
                "missing_artifact_expected_key", "missing_artifact_digest",
            }
        },
        "evidence_ref": _map(source.get("evidence_ref")),
    }
    source_counts = {
        "acceptance_findings": len(list(source.get("acceptance_findings") or [])),
        "failing_checks": len(list(source.get("failing_checks") or [])),
        "required_status_contexts": len(
            list(source.get("required_status_contexts") or [])
        ),
    }
    included_counts = {
        "acceptance_findings": len(findings),
        "failing_checks": len(checks),
        "required_status_contexts": len(required),
    }
    tape["tape_bounds"] = {
        "max_bytes": MISSION_TAPE_MAX_BYTES,
        "max_items_per_surface": MISSION_TAPE_MAX_ITEMS,
        "source_counts": source_counts,
        "included_counts": included_counts,
        "truncated": source_counts != included_counts,
        "redaction_notice": "<redacted>",
    }
    encoded = json.dumps(
        tape, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    if len(encoded) > MISSION_TAPE_MAX_BYTES:
        # Preserve the mission, exact identities, findings, and durable lookup.
        # Large prose is optional because the full record remains addressable.
        tape["task"].pop("description", None)
        tape["task"].pop("entry_criteria", None)
        tape["task"].pop("exit_criteria", None)
        tape["github_pr"].pop("body", None)
        tape["review"].pop("body", None)
        tape["tape_bounds"]["truncated"] = True
        tape["tape_bounds"]["prose_omitted"] = True
        encoded = json.dumps(
            tape, sort_keys=True, separators=(",", ":"), default=str,
        ).encode()
    if len(encoded) > MISSION_TAPE_MAX_BYTES:
        raise ValueError(
            f"mission_tape_exceeds_budget:{len(encoded)}:"
            f"{MISSION_TAPE_MAX_BYTES}"
        )
    tape["tape_bounds"]["actual_bytes"] = len(encoded)
    encoded = json.dumps(
        tape, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    tape["tape_bounds"]["actual_bytes"] = len(encoded)
    if len(encoded) > MISSION_TAPE_MAX_BYTES:
        raise ValueError(
            f"mission_tape_exceeds_budget:{len(encoded)}:"
            f"{MISSION_TAPE_MAX_BYTES}"
        )
    return tape


def prompt_safe_dossier(dossier: Mapping[str, Any]) -> dict[str, Any]:
    """Return one bounded, redacted tape plus a durable full-evidence reference."""
    if not isinstance(dossier, Mapping):
        return {}
    return _mission_tape(dossier)


def build_dossier(
    snapshot: Mapping[str, Any],
    *,
    reason_code: str,
    mission: str,
) -> dict[str, Any]:
    """Copy live facts into the mission dossier without compression."""
    snap = _map(snapshot)
    findings = _finding_rows(snap.get("findings"))
    review = _map(snap.get("review"))
    for item in _finding_rows(review.get("findings")):
        findings.append(item)
    failing_checks = _failing_checks(snap)
    if failing_checks and not findings:
        for row in failing_checks:
            findings.append({
                "code": reason_code or "required_ci_failed",
                "message": _check_summary(row) or reason_code,
                "finding_class": "automatic",
                "blocking": True,
                "failing_contexts": [
                    str(row.get("context") or row.get("name") or "")
                ],
                "failing_check_url": _check_url(row),
                "failing_run_attempt": _run_attempt(row),
                "failing_check_summary": _check_summary(row),
                "check": row,
            })
    # Attach per-check identity onto findings without dropping nested evidence.
    if findings and failing_checks:
        head = dict(findings[0])
        head.setdefault("failing_contexts", [
            str(item.get("context") or item.get("name") or "")
            for item in failing_checks
        ])
        head.setdefault("failing_check_url", _check_url(failing_checks[0]))
        head.setdefault("failing_run_attempt", _run_attempt(failing_checks[0]))
        head.setdefault("failing_check_summary", _check_summary(failing_checks[0]))
        head.setdefault("failing_checks", failing_checks)
        findings[0] = head
    agent_blocker = _map(_map(_map(snap.get("work_session")).get("hygiene")).get("blocker"))
    missing_artifact = _missing_artifact_from_findings(findings)
    dossier = {
        "schema": MISSION_DOSSIER_SCHEMA,
        "mission": mission,
        "reason_code": reason_code,
        "task_id": _text(snap.get("task_id")).upper(),
        "pr_number": int(snap.get("pr_number") or 0),
        "pr_url": _text_raw(snap.get("pr_url")),
        "head_sha": _text(snap.get("head_sha")).lower(),
        "board_status": _text_raw(snap.get("board_status")),
        # Full nested fact surfaces — unchanged copies (operators / identity).
        "task": _map(snap.get("task")),
        "github_pr": _map(snap.get("github_pr")),
        "required_status_contexts": list(snap.get("required_status_contexts") or []),
        "status_contexts": _map(snap.get("status_contexts")),
        "review": review,
        "merge_gate": _map(snap.get("merge_gate")),
        "merge_queue": _map(snap.get("merge_queue")),
        "merge_provenance": _map(snap.get("merge_provenance")),
        "work_session": _map(snap.get("work_session")),
        "runner": _map(snap.get("runner")),
        "dependency_state": _map(
            snap.get("dependency_state")
            or _map(snap.get("task")).get("dependency_state")
        ),
        "acceptance_findings": findings,
        "failing_checks": failing_checks,
        "missing_artifact": missing_artifact or None,
        "failing_contexts": [
            str(item.get("context") or item.get("name") or "")
            for item in failing_checks
        ],
        "failing_check_url": (
            _check_url(failing_checks[0]) if failing_checks else ""
        ),
        "failing_run_attempt": (
            _run_attempt(failing_checks[0]) if failing_checks else 0
        ),
        "failing_check_summary": (
            _check_summary(failing_checks[0]) if failing_checks else ""
        ),
        "agent_blocker": agent_blocker or None,
    }
    dossier["evidence_identity"] = evidence_identity(dossier)
    return dossier


def _missing_artifact_from_findings(
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Lift the gate's missing_artifact report into the dossier unchanged.

    Reads the live gate shape only: ``_merge_gate_finding`` splats details onto
    the finding top level. Nested ``details`` blobs are intentionally ignored
    (BUG-194) so a second reader cannot reintroduce the COORD-61 discard.
    """
    for item in findings:
        row = _map(item)
        direct = _map(finding_detail(row, "missing_artifact"))
        if direct:
            return direct
        nested = _map(
            _map(finding_detail(row, "executed_test_gate")).get("missing_artifact")
        )
        if nested:
            return nested
    return {}


__all__ = ["build_dossier", "evidence_identity", "prompt_safe_dossier"]
