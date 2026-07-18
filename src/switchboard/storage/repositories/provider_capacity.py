"""Persistent subscription-capacity and checkpoint state for CO-8.

The registry is the durable boundary shared by coordinators and workers.  Provider payloads
are normalized before this repository writes anything; raw errors and credentials are never
stored or returned.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Mapping, Optional

from db.core import _registry_conn
from db.schema import init_project_registry
from switchboard.domain.provider_capacity import (
    POLLABLE_CAPACITY_STATES,
    ProviderCapacitySignal,
    account_fingerprint,
    evaluate_metered_lane_policy,
    normalize_provider_response,
)
from switchboard.domain.provider_credentials import (
    CredentialPolicyError,
    auth_host_classes_for_host,
    normalize_provider,
    provider_auth_decision,
)
from switchboard.storage.repositories.provider_credentials import (
    CredentialVaultError,
    ProviderCredentialRepository,
)


PROVIDER_CAPACITY_ACCOUNT_SCHEMA = "switchboard.provider_capacity.account.v1"
PROVIDER_CAPACITY_CHECKPOINT_SCHEMA = "switchboard.provider_capacity.checkpoint.v1"
PROVIDER_CAPACITY_POLL_SCHEMA = "switchboard.provider_capacity.poll.v1"
PROVIDER_CAPACITY_DECISION_SCHEMA = "switchboard.provider_capacity.decision.v1"
DEFAULT_POLL_INTERVAL_SECONDS = 60
MAX_POLL_INTERVAL_SECONDS = 900
DEFAULT_MAX_POLLS_PER_WINDOW = 8
DEFAULT_POLL_WINDOW_SECONDS = 3600
DEFAULT_POLL_LEASE_SECONDS = 60

_CHECKPOINT_FIELDS = frozenset({
    "branch", "continuation_ref", "head_sha", "remote_ref",
    "test_artifact_id",
})
_EVENT_DETAIL_FIELDS = frozenset({
    "attempt", "budget_id", "cost_center", "currency", "idempotent_replay",
    "lane_kind", "max_parallel", "poll_id", "state_version",
})


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _safe_checkpoint(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        if key not in _CHECKPOINT_FIELDS or not isinstance(item, (str, int, float, bool)):
            continue
        result[key] = str(item)[:512] if isinstance(item, str) else item
    return result


def _safe_event_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        if key in _EVENT_DETAIL_FIELDS and isinstance(item, (str, int, float, bool)):
            result[key] = str(item)[:128] if isinstance(item, str) else item
    return result


class ProviderCapacityRepository:
    """Account capacity, task checkpoint, and bounded-poll persistence."""

    def _prepare(self) -> None:
        init_project_registry()

    @staticmethod
    def _normalize_binding(
        binding: Mapping[str, Any], *, require_execution_binding: bool = True,
    ) -> dict[str, str]:
        raw = dict(binding or {})
        try:
            provider = normalize_provider(str(raw.get("provider") or ""))
        except CredentialPolicyError as exc:
            raise CredentialVaultError(exc.code, exc.message) from exc
        normalized = {
            "credential_reference": str(raw.get("credential_reference") or "").strip(),
            "execution_connection_id": str(
                raw.get("execution_connection_id")
                or raw.get("credential_reference") or ""
            ).strip(),
            "project_id": str(raw.get("project") or raw.get("project_id") or "").strip().lower(),
            "tenant_id": str(raw.get("tenant_id") or "").strip(),
            "user_id": str(raw.get("user_id") or "").strip(),
            "provider": provider,
            "provider_account_id": str(raw.get("provider_account_id") or "").strip(),
            "task_id": str(raw.get("task_id") or "").strip(),
            "claim_id": str(raw.get("claim_id") or "").strip(),
            "host_id": str(raw.get("host_id") or "").strip(),
            "runner_session_id": str(raw.get("runner_session_id") or "").strip(),
            "work_session_id": str(raw.get("work_session_id") or "").strip(),
        }
        required = (
            "credential_reference", "project_id", "user_id", "provider",
            "provider_account_id", "task_id",
        )
        if require_execution_binding:
            required += ("claim_id", "host_id", "runner_session_id", "work_session_id")
        if any(not normalized[key] for key in required):
            raise CredentialVaultError(
                "provider_capacity_binding_incomplete",
                "provider capacity identity binding is incomplete",
            )
        return normalized

    @staticmethod
    def _connection_in(c, binding: Mapping[str, str]) -> dict[str, Any]:
        row = c.execute(
            "SELECT * FROM provider_connections WHERE credential_reference=?",
            (binding["credential_reference"],),
        ).fetchone()
        if not row:
            raise CredentialVaultError(
                "credential_not_available", "provider credential is not available", status_code=404)
        connection = dict(row)
        tenant_id = ProviderCredentialRepository._tenant_for_project_in(
            c, binding["project_id"])
        exact = (
            connection.get("tenant_id") == tenant_id
            and (not binding.get("tenant_id") or binding.get("tenant_id") == tenant_id)
            and connection.get("user_id") == binding["user_id"]
            and connection.get("provider") == binding["provider"]
            and connection.get("provider_account_id") == binding["provider_account_id"]
            and connection.get("credential_reference")
            == binding.get("execution_connection_id")
            and binding["project_id"] in _json_list(connection.get("project_allowlist_json"))
        )
        if not exact:
            raise CredentialVaultError(
                "provider_capacity_binding_mismatch",
                "provider capacity identity binding failed",
                status_code=403,
            )
        return connection

    @staticmethod
    def _next_poll(signal: ProviderCapacitySignal, *, now: float) -> float | None:
        if signal.state not in POLLABLE_CAPACITY_STATES:
            return None
        delay = max(
            DEFAULT_POLL_INTERVAL_SECONDS,
            min(int(signal.retry_after_seconds or DEFAULT_POLL_INTERVAL_SECONDS),
                MAX_POLL_INTERVAL_SECONDS),
        )
        return now + delay

    @staticmethod
    def _public_account(row: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        return {
            "schema": PROVIDER_CAPACITY_ACCOUNT_SCHEMA,
            "execution_connection_id": item.get("credential_reference"),
            "user_id": item.get("user_id"),
            "provider": item.get("provider"),
            "provider_account": account_fingerprint(
                str(item.get("provider") or ""),
                str(item.get("provider_account_id") or ""),
            ),
            "state": item.get("state"),
            "reason_code": item.get("reason_code"),
            "retry_after_seconds": item.get("retry_after_seconds"),
            "reset_at": item.get("reset_at"),
            "next_poll_at": item.get("next_poll_at"),
            "cooldown_until": item.get("cooldown_until"),
            "poll_attempts": int(item.get("poll_attempts") or 0),
            "state_version": int(item.get("state_version") or 0),
            "observed_at": item.get("observed_at"),
            "updated_at": item.get("updated_at"),
        }

    @staticmethod
    def _public_checkpoint(row: Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        return {
            "schema": PROVIDER_CAPACITY_CHECKPOINT_SCHEMA,
            "checkpoint_id": item.get("checkpoint_id"),
            "execution_connection_id": item.get("credential_reference"),
            "provider": item.get("provider"),
            "provider_account": account_fingerprint(
                str(item.get("provider") or ""),
                str(item.get("provider_account_id") or ""),
            ),
            "project": item.get("project_id"),
            "task_id": item.get("task_id"),
            "claim_id": item.get("claim_id"),
            "host_id": item.get("host_id"),
            "runner_session_id": item.get("runner_session_id"),
            "work_session_id": item.get("work_session_id"),
            "state": item.get("state"),
            "reason_code": item.get("reason_code"),
            "status": item.get("status"),
            "checkpoint": _json_object(item.get("checkpoint_json")),
            "retry_after_seconds": item.get("retry_after_seconds"),
            "reset_at": item.get("reset_at"),
            "next_retry_at": item.get("next_retry_at"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "resumed_at": item.get("resumed_at"),
        }

    @staticmethod
    def _event_in(c, connection: Mapping[str, Any], binding: Mapping[str, str],
                  signal: ProviderCapacitySignal, *, actor: str,
                  details: Mapping[str, Any] | None = None, now: float) -> None:
        c.execute(
            "INSERT INTO provider_capacity_events("
            "event_id, credential_reference, tenant_id, user_id, provider, "
            "provider_account_id, project_id, task_id, claim_id, work_session_id, "
            "state, reason_code, actor, details_json, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"provider-capacity-event-{uuid.uuid4().hex[:16]}",
                connection["credential_reference"], connection["tenant_id"],
                connection["user_id"], connection["provider"],
                connection["provider_account_id"], binding["project_id"],
                binding["task_id"], binding["claim_id"], binding["work_session_id"],
                signal.state, signal.reason_code, str(actor or "system"),
                json.dumps(_safe_event_details(details), sort_keys=True), now,
            ),
        )

    def _apply_signal_in(
        self,
        c,
        connection: Mapping[str, Any],
        binding: Mapping[str, str],
        signal: ProviderCapacitySignal,
        *,
        checkpoint: Mapping[str, Any] | None,
        actor: str,
        now: float,
        details: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        previous = c.execute(
            "SELECT * FROM provider_capacity_accounts WHERE credential_reference=?",
            (connection["credential_reference"],),
        ).fetchone()
        changed_state = not previous or previous["state"] != signal.state \
            or previous["reason_code"] != signal.reason_code
        version = int(previous["state_version"] or 0) + 1 if previous else 1
        attempts = 0 if signal.state == "ready" or changed_state else int(previous["poll_attempts"] or 0)
        window_started = None if signal.state == "ready" else (
            previous["poll_window_started_at"] if previous and not changed_state else now)
        next_poll = self._next_poll(signal, now=now)
        cooldown_until = next_poll if signal.state in POLLABLE_CAPACITY_STATES else None
        c.execute(
            "INSERT INTO provider_capacity_accounts("
            "credential_reference, tenant_id, user_id, provider, provider_account_id, "
            "state, reason_code, retry_after_seconds, reset_at, next_poll_at, "
            "cooldown_until, poll_attempts, poll_window_started_at, state_version, "
            "observed_at, updated_at, updated_by"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(credential_reference) DO UPDATE SET "
            "tenant_id=excluded.tenant_id, user_id=excluded.user_id, provider=excluded.provider, "
            "provider_account_id=excluded.provider_account_id, state=excluded.state, "
            "reason_code=excluded.reason_code, retry_after_seconds=excluded.retry_after_seconds, "
            "reset_at=excluded.reset_at, next_poll_at=excluded.next_poll_at, "
            "cooldown_until=excluded.cooldown_until, poll_attempts=excluded.poll_attempts, "
            "poll_window_started_at=excluded.poll_window_started_at, "
            "state_version=excluded.state_version, observed_at=excluded.observed_at, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (
                connection["credential_reference"], connection["tenant_id"],
                connection["user_id"], connection["provider"],
                connection["provider_account_id"], signal.state, signal.reason_code,
                signal.retry_after_seconds, signal.reset_at, next_poll, cooldown_until,
                attempts, window_started, version, signal.observed_at, now,
                str(actor or "system"),
            ),
        )

        checkpoint_row = None
        if signal.state == "ready":
            c.execute(
                "UPDATE provider_capacity_checkpoints SET status='resume_ready', state='ready', "
                "reason_code='provider_ready', retry_after_seconds=NULL, reset_at=NULL, "
                "next_retry_at=NULL, resumed_at=?, updated_at=? "
                "WHERE credential_reference=? AND status='paused'",
                (now, now, connection["credential_reference"]),
            )
            checkpoint_row = c.execute(
                "SELECT * FROM provider_capacity_checkpoints WHERE credential_reference=? "
                "AND project_id=? AND task_id=? AND claim_id=? AND work_session_id=?",
                (
                    connection["credential_reference"], binding["project_id"],
                    binding["task_id"], binding["claim_id"], binding["work_session_id"],
                ),
            ).fetchone()
        else:
            safe_checkpoint = _safe_checkpoint(checkpoint)
            checkpoint_id = f"provider-checkpoint-{uuid.uuid4().hex[:16]}"
            next_retry = next_poll
            c.execute(
                "INSERT INTO provider_capacity_checkpoints("
                "checkpoint_id, credential_reference, tenant_id, user_id, provider, "
                "provider_account_id, project_id, task_id, claim_id, host_id, "
                "runner_session_id, work_session_id, state, reason_code, status, "
                "checkpoint_json, retry_after_seconds, reset_at, next_retry_at, "
                "created_at, updated_at, resumed_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) "
                "ON CONFLICT(credential_reference, project_id, task_id, claim_id, work_session_id) "
                "DO UPDATE SET host_id=excluded.host_id, runner_session_id=excluded.runner_session_id, "
                "state=excluded.state, reason_code=excluded.reason_code, status='paused', "
                "checkpoint_json=excluded.checkpoint_json, "
                "retry_after_seconds=excluded.retry_after_seconds, reset_at=excluded.reset_at, "
                "next_retry_at=excluded.next_retry_at, updated_at=excluded.updated_at, resumed_at=NULL",
                (
                    checkpoint_id, connection["credential_reference"], connection["tenant_id"],
                    connection["user_id"], connection["provider"],
                    connection["provider_account_id"], binding["project_id"],
                    binding["task_id"], binding["claim_id"], binding["host_id"],
                    binding["runner_session_id"], binding["work_session_id"],
                    signal.state, signal.reason_code, "paused",
                    json.dumps(safe_checkpoint, sort_keys=True), signal.retry_after_seconds,
                    signal.reset_at, next_retry, now, now,
                ),
            )
            checkpoint_row = c.execute(
                "SELECT * FROM provider_capacity_checkpoints WHERE credential_reference=? "
                "AND project_id=? AND task_id=? AND claim_id=? AND work_session_id=?",
                (
                    connection["credential_reference"], binding["project_id"],
                    binding["task_id"], binding["claim_id"], binding["work_session_id"],
                ),
            ).fetchone()

        self._event_in(
            c, connection, binding, signal, actor=actor, details=details, now=now)
        account = c.execute(
            "SELECT * FROM provider_capacity_accounts WHERE credential_reference=?",
            (connection["credential_reference"],),
        ).fetchone()
        return dict(account), (dict(checkpoint_row) if checkpoint_row else None)

    def observe(
        self,
        binding: Mapping[str, Any],
        response: Mapping[str, Any] | None,
        *,
        checkpoint: Mapping[str, Any] | None,
        actor: str,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        self._prepare()
        timestamp = time.time() if now is None else float(now)
        normalized = self._normalize_binding(binding)
        signal = normalize_provider_response(
            normalized["provider"], response, now=timestamp)
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            connection = self._connection_in(c, normalized)
            account, checkpoint_row = self._apply_signal_in(
                c, connection, normalized, signal, checkpoint=checkpoint,
                actor=actor, now=timestamp,
            )
            return {
                "account": self._public_account(account),
                "checkpoint": self._public_checkpoint(checkpoint_row) if checkpoint_row else None,
            }

    def get_state(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        self._prepare()
        normalized = self._normalize_binding(binding)
        with _registry_conn() as c:
            connection = self._connection_in(c, normalized)
            account = c.execute(
                "SELECT * FROM provider_capacity_accounts WHERE credential_reference=?",
                (connection["credential_reference"],),
            ).fetchone()
            checkpoint = c.execute(
                "SELECT * FROM provider_capacity_checkpoints WHERE credential_reference=? "
                "AND project_id=? AND task_id=? AND claim_id=? AND work_session_id=?",
                (
                    connection["credential_reference"], normalized["project_id"],
                    normalized["task_id"], normalized["claim_id"],
                    normalized["work_session_id"],
                ),
            ).fetchone()
            if not account:
                return {
                    "account": {
                        "schema": PROVIDER_CAPACITY_ACCOUNT_SCHEMA,
                        "user_id": connection["user_id"],
                        "provider": connection["provider"],
                        "provider_account": account_fingerprint(
                            connection["provider"], connection["provider_account_id"]),
                        "state": "ready",
                        "reason_code": "no_capacity_restriction_observed",
                        "retry_after_seconds": None,
                        "reset_at": None,
                        "next_poll_at": None,
                        "cooldown_until": None,
                        "poll_attempts": 0,
                        "state_version": 0,
                    },
                    "checkpoint": self._public_checkpoint(checkpoint) if checkpoint else None,
                }
            return {
                "account": self._public_account(account),
                "checkpoint": self._public_checkpoint(checkpoint) if checkpoint else None,
            }

    def admission_decision(
        self,
        binding: Mapping[str, Any],
        *,
        task_policy: Mapping[str, Any] | None = None,
        lane_policy: Mapping[str, Any] | None = None,
        host_available: bool = True,
        require_execution_binding: bool = True,
        exclude_lease_id: str = "",
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        self._prepare()
        timestamp = time.time() if now is None else float(now)
        normalized = self._normalize_binding(
            binding, require_execution_binding=require_execution_binding)
        task = dict(task_policy or {})
        connection: dict[str, Any] = {}

        def decision(allowed: bool, state: str, reason: str, **extra: Any) -> dict[str, Any]:
            return {
                "schema": PROVIDER_CAPACITY_DECISION_SCHEMA,
                "allowed": allowed,
                "state": state,
                "reason_code": reason,
                "execution_connection_id": normalized["execution_connection_id"],
                "connection_kind": str(
                    connection.get("connection_kind") or "personal_subscription"),
                "provider": normalized["provider"],
                "provider_account": account_fingerprint(
                    normalized["provider"], normalized["provider_account_id"]),
                **extra,
            }

        with _registry_conn() as c:
            connection = self._connection_in(c, normalized)
            expected_user = str(task.get("customer_user_id") or normalized["user_id"]).strip()
            if expected_user != normalized["user_id"]:
                return decision(False, "policy_blocked", "cross_customer_account_denied")
            try:
                requested = normalize_provider(
                    str(task.get("requested_provider") or normalized["provider"]))
            except CredentialPolicyError:
                return decision(False, "policy_blocked", "requested_provider_not_supported")
            if requested != normalized["provider"]:
                allowed_providers = set()
                raw_allowed = task.get("allowed_providers") or []
                if not isinstance(raw_allowed, (list, tuple, set)):
                    raw_allowed = []
                for item in raw_allowed:
                    try:
                        allowed_providers.add(normalize_provider(str(item)))
                    except CredentialPolicyError:
                        continue
                if task.get("allow_provider_substitution") is not True \
                        or normalized["provider"] not in allowed_providers:
                    return decision(False, "policy_blocked", "provider_substitution_not_permitted")

            host_id = str(normalized.get("host_id") or "").strip()
            # Classify from host_id + optional placement advertisement in the binding.
            # Do not trust a bare caller host_class field.
            host_classes = auth_host_classes_for_host({
                "host_id": host_id,
                "capacity": {
                    "placement": dict(
                        dict(binding or {}).get("host_placement")
                        or dict(binding or {}).get("placement")
                        or {}),
                },
            })
            auth_policy = provider_auth_decision(
                str(connection.get("provider") or ""),
                str(connection.get("auth_type") or ""),
                host_classes=host_classes,
                operation="schedule",
                now=timestamp,
            )
            if not auth_policy.get("allowed"):
                return decision(
                    False,
                    str(auth_policy.get("state") or "policy_blocked"),
                    str(auth_policy.get("reason_code") or "provider_auth_policy_denied"),
                    auth_mode=auth_policy.get("auth_mode"),
                    approval_state=auth_policy.get("approval_state"),
                )

            lane = evaluate_metered_lane_policy(
                lane_policy, active_credential_reference=normalized["credential_reference"])
            if not lane["allowed"]:
                return decision(False, "policy_blocked", str(lane["reason_code"]),
                                lane_kind=lane.get("lane_kind"))
            if lane.get("metered"):
                personal_reference = str(
                    dict(lane_policy or {}).get("personal_credential_reference") or "").strip()
                personal = c.execute(
                    "SELECT * FROM provider_connections WHERE credential_reference=?",
                    (personal_reference,),
                ).fetchone()
                if not personal or personal["tenant_id"] != connection["tenant_id"] \
                        or personal["user_id"] != connection["user_id"] \
                        or personal["lifecycle_state"] != "active":
                    return decision(False, "policy_blocked", "separate_customer_credential_invalid",
                                    lane_kind="metered")

            lifecycle = str(connection.get("lifecycle_state") or "")
            revocation = str(connection.get("revocation_state") or "")
            if lifecycle == "revoked" or revocation == "revoked":
                return decision(False, "revoked", "provider_credential_revoked")
            if (lifecycle != "active"
                    or (not connection.get("encrypted_credential")
                        and connection.get("materialization_mode") != "host_native")):
                return decision(False, "reauthentication_required", "provider_credential_not_active")

            account = c.execute(
                "SELECT * FROM provider_capacity_accounts WHERE credential_reference=?",
                (connection["credential_reference"],),
            ).fetchone()
            if account and account["state"] != "ready":
                public = self._public_account(account)
                return decision(
                    False, str(account["state"]), str(account["reason_code"]),
                    retry_after_seconds=account["retry_after_seconds"],
                    reset_at=account["reset_at"], next_poll_at=account["next_poll_at"],
                    poll_due=bool(account["next_poll_at"] and account["next_poll_at"] <= timestamp),
                    state_version=public["state_version"],
                )

            concurrency = _json_object(connection.get("concurrency_policy_json"))
            forced = auth_policy.get("forced_concurrency_policy") or {}
            maximum = int(
                forced.get("max_parallel")
                or concurrency.get("max_parallel")
                or 1
            )
            active = c.execute(
                "SELECT COUNT(*), MIN(expires_at) FROM provider_credential_leases "
                "WHERE credential_reference=? AND state IN ('issued','materializing','active') "
                "AND expires_at>? AND (?='' OR lease_id<>?)",
                (
                    connection["credential_reference"], timestamp,
                    str(exclude_lease_id or "").strip(),
                    str(exclude_lease_id or "").strip(),
                ),
            ).fetchone()
            if int(active[0] or 0) >= maximum:
                retry = max(1, int(float(active[1] or timestamp + 60) - timestamp))
                return decision(
                    False, "provider_capacity_exhausted",
                    "provider_account_concurrency_limit",
                    retry_after_seconds=retry, max_parallel=maximum,
                )
            if not host_available:
                return decision(
                    False, "provider_capacity_exhausted", "host_capacity_unavailable")
            return decision(
                True, "ready", str(lane["reason_code"]),
                lane_kind=lane.get("lane_kind"), metered=bool(lane.get("metered")),
                budget_ceiling=lane.get("budget_ceiling"),
                cost_attribution=lane.get("cost_attribution"),
                max_parallel=maximum,
            )

    def begin_poll(
        self,
        binding: Mapping[str, Any],
        *,
        idem_key: str,
        actor: str,
        now: Optional[float] = None,
        max_attempts: int = DEFAULT_MAX_POLLS_PER_WINDOW,
        window_seconds: int = DEFAULT_POLL_WINDOW_SECONDS,
        lease_seconds: int = DEFAULT_POLL_LEASE_SECONDS,
    ) -> dict[str, Any]:
        self._prepare()
        timestamp = time.time() if now is None else float(now)
        normalized = self._normalize_binding(binding)
        key = str(idem_key or "").strip()
        if not key:
            raise CredentialVaultError(
                "capacity_poll_idempotency_required", "capacity poll idempotency key is required")
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            connection = self._connection_in(c, normalized)
            prior = c.execute(
                "SELECT * FROM provider_capacity_polls WHERE credential_reference=? AND idem_key=?",
                (connection["credential_reference"], key),
            ).fetchone()
            if prior and prior["status"] == "completed":
                receipt = _json_object(prior["receipt_json"])
                return {**receipt, "execute_probe": False, "idempotent_replay": True}
            account = c.execute(
                "SELECT * FROM provider_capacity_accounts WHERE credential_reference=?",
                (connection["credential_reference"],),
            ).fetchone()
            if not account or account["state"] not in POLLABLE_CAPACITY_STATES:
                receipt = {
                    "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                    "allowed": False, "execute_probe": False,
                    "reason_code": "capacity_state_not_pollable",
                }
                if prior:
                    receipt["poll_id"] = prior["poll_id"]
                    c.execute(
                        "UPDATE provider_capacity_polls SET status='completed', completed_at=?, "
                        "receipt_json=? WHERE poll_id=? AND status='started'",
                        (timestamp, json.dumps(receipt, sort_keys=True), prior["poll_id"]),
                    )
                return receipt
            attempts = int(account["poll_attempts"] or 0)
            window_started = float(account["poll_window_started_at"] or timestamp)
            if timestamp - window_started >= max(1, int(window_seconds)):
                attempts = 0
                window_started = timestamp
            next_poll_at = float(account["next_poll_at"] or 0)
            if prior:
                prior_attempt = int(prior["attempt"] or 0)
                if prior["status"] != "started":
                    return {
                        "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                        "poll_id": prior["poll_id"],
                        "allowed": False,
                        "execute_probe": False,
                        "reason_code": "capacity_poll_invalid_state",
                    }
                if float(prior["lease_expires_at"] or 0) > timestamp:
                    return {
                        **_json_object(prior["receipt_json"]),
                        "allowed": False,
                        "execute_probe": False,
                        "idempotent_replay": True,
                        "in_flight": True,
                        "reason_code": "capacity_poll_in_flight",
                        "lease_expires_at": prior["lease_expires_at"],
                    }
                if int(prior["state_version"] or 0) != int(account["state_version"] or 0):
                    receipt = {
                        "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                        "poll_id": prior["poll_id"],
                        "allowed": False,
                        "execute_probe": False,
                        "reason_code": "capacity_poll_stale",
                        "attempt": prior_attempt,
                    }
                    c.execute(
                        "UPDATE provider_capacity_polls SET status='completed', completed_at=?, "
                        "receipt_json=? WHERE poll_id=? AND status='started' AND attempt=?",
                        (
                            timestamp, json.dumps(receipt, sort_keys=True),
                            prior["poll_id"], prior_attempt,
                        ),
                    )
                    return receipt
                if next_poll_at > timestamp:
                    return {
                        **_json_object(prior["receipt_json"]),
                        "allowed": False,
                        "execute_probe": False,
                        "idempotent_replay": True,
                        "reason_code": "capacity_poll_reclaim_not_due",
                        "next_poll_at": account["next_poll_at"],
                    }
                if attempts >= max(1, int(max_attempts)):
                    receipt = {
                        "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                        "poll_id": prior["poll_id"],
                        "allowed": False,
                        "execute_probe": False,
                        "reason_code": "capacity_poll_budget_exhausted",
                        "attempt": prior_attempt,
                        "next_poll_at": account["next_poll_at"],
                    }
                    c.execute(
                        "UPDATE provider_capacity_polls SET status='completed', completed_at=?, "
                        "receipt_json=? WHERE poll_id=? AND status='started' AND attempt=?",
                        (
                            timestamp, json.dumps(receipt, sort_keys=True),
                            prior["poll_id"], prior_attempt,
                        ),
                    )
                    return receipt
                window_attempt = attempts + 1
                poll_attempt = prior_attempt + 1
                delay = min(
                    MAX_POLL_INTERVAL_SECONDS,
                    DEFAULT_POLL_INTERVAL_SECONDS * (2 ** min(window_attempt - 1, 4)),
                )
                lease_expires_at = timestamp + max(1, int(lease_seconds))
                receipt = {
                    "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                    "poll_id": prior["poll_id"],
                    "allowed": True,
                    "execute_probe": True,
                    "reason_code": "capacity_poll_reclaimed",
                    "attempt": poll_attempt,
                    "window_attempt": window_attempt,
                    "state_version": int(account["state_version"] or 0),
                    "lease_expires_at": lease_expires_at,
                    "reclaimed": True,
                }
                c.execute(
                    "UPDATE provider_capacity_accounts SET poll_attempts=?, "
                    "poll_window_started_at=?, next_poll_at=?, updated_at=?, updated_by=? "
                    "WHERE credential_reference=?",
                    (
                        window_attempt, window_started, timestamp + delay, timestamp,
                        str(actor or "system"), connection["credential_reference"],
                    ),
                )
                c.execute(
                    "UPDATE provider_capacity_polls SET state_version=?, attempt=?, status='started', "
                    "requested_at=?, lease_expires_at=?, completed_at=NULL, receipt_json=? "
                    "WHERE poll_id=? AND status='started' AND attempt=?",
                    (
                        account["state_version"], poll_attempt, timestamp, lease_expires_at,
                        json.dumps(receipt, sort_keys=True), prior["poll_id"], prior_attempt,
                    ),
                )
                return receipt
            reason = ""
            if next_poll_at > timestamp:
                reason = "capacity_poll_not_due"
            elif attempts >= max(1, int(max_attempts)):
                reason = "capacity_poll_budget_exhausted"
            if reason:
                poll_id = f"provider-poll-{uuid.uuid4().hex[:16]}"
                receipt = {
                    "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                    "poll_id": poll_id, "allowed": False, "execute_probe": False,
                    "reason_code": reason, "attempt": attempts,
                    "next_poll_at": account["next_poll_at"],
                }
                c.execute(
                    "INSERT INTO provider_capacity_polls("
                    "poll_id, credential_reference, idem_key, state_version, attempt, status, "
                    "requested_at, lease_expires_at, completed_at, receipt_json"
                    ") VALUES (?,?,?,?,?,'completed',?,?,?,?)",
                    (
                        poll_id, connection["credential_reference"], key,
                        account["state_version"], attempts, timestamp, timestamp,
                        timestamp, json.dumps(receipt, sort_keys=True),
                    ),
                )
                return receipt
            attempt = attempts + 1
            delay = min(
                MAX_POLL_INTERVAL_SECONDS,
                DEFAULT_POLL_INTERVAL_SECONDS * (2 ** min(attempt - 1, 4)),
            )
            poll_id = f"provider-poll-{uuid.uuid4().hex[:16]}"
            lease_expires_at = timestamp + max(1, int(lease_seconds))
            receipt = {
                "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                "poll_id": poll_id, "allowed": True, "execute_probe": True,
                "reason_code": "capacity_poll_due", "attempt": attempt,
                "state_version": int(account["state_version"] or 0),
                "lease_expires_at": lease_expires_at,
            }
            c.execute(
                "UPDATE provider_capacity_accounts SET poll_attempts=?, "
                "poll_window_started_at=?, next_poll_at=?, updated_at=?, updated_by=? "
                "WHERE credential_reference=?",
                (
                    attempt, window_started, timestamp + delay, timestamp,
                    str(actor or "system"), connection["credential_reference"],
                ),
            )
            c.execute(
                "INSERT INTO provider_capacity_polls("
                "poll_id, credential_reference, idem_key, state_version, attempt, status, "
                "requested_at, lease_expires_at, completed_at, receipt_json"
                ") VALUES (?,?,?,?,?,'started',?,?,NULL,?)",
                (
                    poll_id, connection["credential_reference"], key,
                    account["state_version"], attempt, timestamp,
                    lease_expires_at, json.dumps(receipt, sort_keys=True),
                ),
            )
            return receipt

    def complete_poll(
        self,
        binding: Mapping[str, Any],
        *,
        poll_id: str,
        attempt: int,
        response: Mapping[str, Any] | None,
        checkpoint: Mapping[str, Any] | None,
        actor: str,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        self._prepare()
        timestamp = time.time() if now is None else float(now)
        normalized = self._normalize_binding(binding)
        with _registry_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            connection = self._connection_in(c, normalized)
            poll = c.execute(
                "SELECT * FROM provider_capacity_polls WHERE poll_id=? AND credential_reference=?",
                (str(poll_id or "").strip(), connection["credential_reference"]),
            ).fetchone()
            if not poll:
                raise CredentialVaultError(
                    "capacity_poll_not_available", "capacity poll is not available", status_code=404)
            if poll["status"] == "completed":
                return {**_json_object(poll["receipt_json"]),
                        "execute_probe": False, "idempotent_replay": True}
            expected_attempt = int(attempt)
            current_attempt = int(poll["attempt"] or 0)
            if current_attempt != expected_attempt \
                    or float(poll["lease_expires_at"] or 0) <= timestamp:
                return {
                    "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                    "poll_id": poll["poll_id"],
                    "allowed": False,
                    "execute_probe": False,
                    "reason_code": "capacity_poll_stale",
                    "attempt": expected_attempt,
                }
            current = c.execute(
                "SELECT state_version FROM provider_capacity_accounts "
                "WHERE credential_reference=?",
                (connection["credential_reference"],),
            ).fetchone()
            if not current or int(current["state_version"] or 0) != int(poll["state_version"] or 0):
                receipt = {
                    "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                    "poll_id": poll["poll_id"], "allowed": False,
                    "execute_probe": False, "reason_code": "capacity_poll_stale",
                    "attempt": int(poll["attempt"] or 0),
                }
                c.execute(
                    "UPDATE provider_capacity_polls SET status='completed', completed_at=?, "
                    "receipt_json=? WHERE poll_id=? AND status='started' AND attempt=?",
                    (
                        timestamp, json.dumps(receipt, sort_keys=True),
                        poll["poll_id"], current_attempt,
                    ),
                )
                return receipt
            signal = normalize_provider_response(
                normalized["provider"], response, now=timestamp)
            account, checkpoint_row = self._apply_signal_in(
                c, connection, normalized, signal, checkpoint=checkpoint,
                actor=actor, now=timestamp,
                details={"attempt": int(poll["attempt"] or 0), "poll_id": poll["poll_id"]},
            )
            receipt = {
                "schema": PROVIDER_CAPACITY_POLL_SCHEMA,
                "poll_id": poll["poll_id"], "allowed": True,
                "execute_probe": False, "reason_code": "capacity_poll_completed",
                "attempt": int(poll["attempt"] or 0),
                "account": self._public_account(account),
                "checkpoint": self._public_checkpoint(checkpoint_row) if checkpoint_row else None,
            }
            c.execute(
                "UPDATE provider_capacity_polls SET status='completed', completed_at=?, "
                "receipt_json=? WHERE poll_id=? AND status='started' AND attempt=?",
                (
                    timestamp, json.dumps(receipt, sort_keys=True),
                    poll["poll_id"], current_attempt,
                ),
            )
            return receipt


default_provider_capacity_repository = ProviderCapacityRepository()


__all__ = [
    "DEFAULT_MAX_POLLS_PER_WINDOW",
    "PROVIDER_CAPACITY_ACCOUNT_SCHEMA",
    "PROVIDER_CAPACITY_CHECKPOINT_SCHEMA",
    "PROVIDER_CAPACITY_DECISION_SCHEMA",
    "PROVIDER_CAPACITY_POLL_SCHEMA",
    "ProviderCapacityRepository",
    "default_provider_capacity_repository",
]
