"""Executable contract for ``narration_requested`` events (NARRATE-7).

This module deliberately does not persist or dispatch events.  NARRATE-8 owns the
transactional outbox and NARRATE-9 owns delivery.  It gives both implementations one strict,
dependency-free envelope validator so malformed, cross-project, or revision-regressing work
fails before an LLM call or visible narration write.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from copy import deepcopy
from typing import Any, Dict, Mapping, Optional


NARRATION_EVENT_SCHEMA = "switchboard.narration_requested.v1"
NARRATION_EVENT_TYPE = "narration_requested"
NARRATION_REQUEST_SCOPE = "narration:request"

ENTITY_TYPES = frozenset({"task", "deliverable"})
PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
ATTEMPT_STATES = frozenset({
    "pending", "claimed", "retry_wait", "delivered", "superseded", "dead_letter",
})

_TOP_LEVEL_FIELDS = frozenset({
    "schema", "event_type", "event_id", "project", "entity_type", "entity_id",
    "source_revision", "source_hash", "causal_event", "priority", "requested_at",
    "dedupe_key", "supersedes", "attempt", "authorization", "trace_id",
})
_CAUSAL_FIELDS = frozenset({"event_id", "kind", "occurred_at", "actor_id"})
_SUPERSEDES_FIELDS = frozenset({"event_id", "source_revision"})
_ATTEMPT_FIELDS = frozenset({
    "state", "count", "available_at", "claimed_by", "lease_expires_at", "last_error",
})
_AUTH_FIELDS = frozenset({"principal_id", "decision_id", "scope", "project"})

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SOURCE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEDUPE_KEY = re.compile(r"^nrq:[0-9a-f]{64}$")


class NarrationEventValidationError(ValueError):
    """A fail-closed contract violation with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def canonical_source_hash(source: Any) -> str:
    """Return the contract hash for a JSON-compatible source snapshot."""
    try:
        payload = json.dumps(
            source, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NarrationEventValidationError(
            "malformed_source", f"source snapshot must be canonical JSON: {exc}"
        ) from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def narration_dedupe_key(project: str, entity_type: str, entity_id: str,
                          source_revision: int, source_hash: str,
                          causal_event_id: str) -> str:
    """Derive the only accepted dedupe key for one immutable request revision."""
    material = json.dumps(
        [project, entity_type, entity_id, source_revision, source_hash, causal_event_id],
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return "nrq:" + hashlib.sha256(material).hexdigest()


def build_narration_requested(*, event_id: str, project: str, entity_type: str,
                              entity_id: str, source_revision: int, source_hash: str,
                              causal_event: Mapping[str, Any], requested_at: float,
                              authorization: Mapping[str, Any], trace_id: str,
                              priority: str = "normal",
                              supersedes: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Build and validate an initial pending request envelope.

    Producers still have to derive ``source_revision`` and ``source_hash`` inside the domain
    mutation transaction.  Accepting them here is intentional: this builder cannot make an
    otherwise non-atomic caller atomic.
    """
    causal = dict(causal_event)
    event = {
        "schema": NARRATION_EVENT_SCHEMA,
        "event_type": NARRATION_EVENT_TYPE,
        "event_id": event_id,
        "project": project,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_revision": source_revision,
        "source_hash": source_hash,
        "causal_event": causal,
        "priority": priority,
        "requested_at": requested_at,
        "dedupe_key": narration_dedupe_key(
            project, entity_type, entity_id, source_revision, source_hash,
            str(causal.get("event_id") or ""),
        ),
        "supersedes": dict(supersedes) if supersedes is not None else None,
        "attempt": {
            "state": "pending",
            "count": 0,
            "available_at": requested_at,
            "claimed_by": None,
            "lease_expires_at": None,
            "last_error": None,
        },
        "authorization": dict(authorization),
        "trace_id": trace_id,
    }
    return validate_narration_requested(event, expected_project=project)


def _fail(code: str, message: str) -> None:
    raise NarrationEventValidationError(code, message)


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("malformed_event", f"{field} must be an object")
    return value


def _strict_fields(value: Mapping[str, Any], allowed: frozenset, required: frozenset,
                   field: str) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        _fail("unknown_field", f"{field} contains unknown field(s): {', '.join(unknown)}")
    if missing:
        _fail("missing_field", f"{field} is missing required field(s): {', '.join(missing)}")


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        _fail("malformed_event", f"{field} must be a non-empty safe identifier")
    return value


def _timestamp(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("malformed_event", f"{field} must be a Unix timestamp")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        _fail("malformed_event", f"{field} must be a positive finite Unix timestamp")
    return parsed


def _revision(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail("malformed_event", f"{field} must be an integer >= 1")
    return value


def validate_narration_requested(event: Mapping[str, Any], *,
                                 expected_project: Optional[str] = None,
                                 current_source_revision: Optional[int] = None,
                                 current_source_hash: Optional[str] = None,
                                 now: Optional[float] = None,
                                 max_future_skew_seconds: float = 300.0) -> Dict[str, Any]:
    """Validate and return a defensive copy of a v1 request.

    ``expected_project`` is mandatory at every persistence/consumer boundary even though it is
    optional here for pure schema tooling.  Passing the entity's current revision/hash turns the
    same validator into the worker's pre-provider stale gate.  A regression is a normal
    supersession outcome for queue processing, but it is still rejected as executable work.
    """
    root = _object(event, "event")
    _strict_fields(root, _TOP_LEVEL_FIELDS, _TOP_LEVEL_FIELDS, "event")

    if root["schema"] != NARRATION_EVENT_SCHEMA:
        _fail("unsupported_schema", f"schema must be {NARRATION_EVENT_SCHEMA}")
    if root["event_type"] != NARRATION_EVENT_TYPE:
        _fail("malformed_event", f"event_type must be {NARRATION_EVENT_TYPE}")

    _safe_id(root["event_id"], "event_id")
    project = _safe_id(root["project"], "project")
    if expected_project is not None and project != expected_project:
        _fail("cross_project", f"event project {project!r} does not match {expected_project!r}")

    if root["entity_type"] not in ENTITY_TYPES:
        _fail("malformed_event", "entity_type must be task or deliverable")
    _safe_id(root["entity_id"], "entity_id")
    source_revision = _revision(root["source_revision"], "source_revision")
    if not isinstance(root["source_hash"], str) or not _SOURCE_HASH.fullmatch(root["source_hash"]):
        _fail("malformed_event", "source_hash must be sha256:<64 lowercase hex characters>")

    causal = _object(root["causal_event"], "causal_event")
    _strict_fields(causal, _CAUSAL_FIELDS,
                   frozenset({"event_id", "kind", "occurred_at"}), "causal_event")
    _safe_id(causal["event_id"], "causal_event.event_id")
    _safe_id(causal["kind"], "causal_event.kind")
    occurred_at = _timestamp(causal["occurred_at"], "causal_event.occurred_at")
    if "actor_id" in causal and causal["actor_id"] is not None:
        _safe_id(causal["actor_id"], "causal_event.actor_id")

    if root["priority"] not in PRIORITIES:
        _fail("malformed_event", f"priority must be one of {', '.join(sorted(PRIORITIES))}")
    requested_at = _timestamp(root["requested_at"], "requested_at")
    if requested_at < occurred_at:
        _fail("malformed_event", "requested_at cannot precede causal_event.occurred_at")
    clock = time.time() if now is None else _timestamp(now, "now")
    if requested_at > clock + max_future_skew_seconds:
        _fail("future_event", "requested_at exceeds the allowed clock-skew window")

    expected_dedupe = narration_dedupe_key(
        project, root["entity_type"], root["entity_id"], source_revision,
        root["source_hash"], causal["event_id"],
    )
    if not isinstance(root["dedupe_key"], str) or not _DEDUPE_KEY.fullmatch(root["dedupe_key"]):
        _fail("malformed_event", "dedupe_key must be nrq:<64 lowercase hex characters>")
    if root["dedupe_key"] != expected_dedupe:
        _fail("dedupe_mismatch", "dedupe_key does not match the immutable request fields")

    supersedes = root["supersedes"]
    if supersedes is not None:
        supersedes = _object(supersedes, "supersedes")
        _strict_fields(supersedes, _SUPERSEDES_FIELDS, _SUPERSEDES_FIELDS, "supersedes")
        if _safe_id(supersedes["event_id"], "supersedes.event_id") == root["event_id"]:
            _fail("malformed_event", "an event cannot supersede itself")
        previous_revision = _revision(supersedes["source_revision"],
                                      "supersedes.source_revision")
        if previous_revision >= source_revision:
            _fail("revision_regression", "superseded revision must be older than source_revision")

    attempt = _object(root["attempt"], "attempt")
    _strict_fields(attempt, _ATTEMPT_FIELDS,
                   frozenset({"state", "count", "available_at"}), "attempt")
    state = attempt["state"]
    if state not in ATTEMPT_STATES:
        _fail("malformed_event", f"attempt.state must be one of {', '.join(sorted(ATTEMPT_STATES))}")
    count = attempt["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        _fail("malformed_event", "attempt.count must be an integer >= 0")
    available_at = _timestamp(attempt["available_at"], "attempt.available_at")
    if available_at < requested_at:
        _fail("malformed_event", "attempt.available_at cannot precede requested_at")
    claimed_by = attempt.get("claimed_by")
    lease_expires_at = attempt.get("lease_expires_at")
    if state == "claimed":
        _safe_id(claimed_by, "attempt.claimed_by")
        if _timestamp(lease_expires_at, "attempt.lease_expires_at") <= available_at:
            _fail("malformed_event", "claimed lease must expire after available_at")
    elif claimed_by is not None or lease_expires_at is not None:
        _fail("malformed_event", "only a claimed attempt may carry an active lease")
    if state == "pending" and count != 0:
        _fail("malformed_event", "an initial pending attempt must have count 0")
    if state in {"retry_wait", "dead_letter"} and not attempt.get("last_error"):
        _fail("malformed_event", f"{state} attempt must preserve last_error")
    if attempt.get("last_error") is not None and not isinstance(attempt["last_error"], str):
        _fail("malformed_event", "attempt.last_error must be text or null")

    authorization = _object(root["authorization"], "authorization")
    _strict_fields(authorization, _AUTH_FIELDS, _AUTH_FIELDS, "authorization")
    _safe_id(authorization["principal_id"], "authorization.principal_id")
    _safe_id(authorization["decision_id"], "authorization.decision_id")
    if authorization["scope"] != NARRATION_REQUEST_SCOPE:
        _fail("unauthorized", f"authorization.scope must be {NARRATION_REQUEST_SCOPE}")
    if authorization["project"] != project:
        _fail("cross_project", "authorization project does not match event project")
    _safe_id(root["trace_id"], "trace_id")

    if current_source_revision is not None:
        current_revision = _revision(current_source_revision, "current_source_revision")
        if source_revision < current_revision:
            _fail(
                "revision_regression",
                f"event revision {source_revision} is older than current revision {current_revision}",
            )
        if source_revision == current_revision and current_source_hash is not None \
                and root["source_hash"] != current_source_hash:
            _fail("revision_collision", "equal source revisions carry different hashes")

    return deepcopy(dict(root))


def request_disposition(event: Mapping[str, Any], *, expected_project: str,
                        current_source_revision: int, current_source_hash: str) -> str:
    """Classify a stored request before provider work: ``ready``, ``current``, or ``stale``.

    ``current`` means the entity is still exactly the snapshot requested and can be generated.
    ``ready`` means the request is for a newer revision than the supplied snapshot (the caller
    should refresh its entity read before continuing).  ``stale`` is suppressed without a
    provider call and should transition to the durable ``superseded`` attempt state.
    """
    validated = validate_narration_requested(event, expected_project=expected_project)
    revision = validated["source_revision"]
    if revision < current_source_revision:
        return "stale"
    if revision == current_source_revision:
        return "current" if validated["source_hash"] == current_source_hash else "stale"
    return "ready"
