"""PROTO-7 durable attention lifecycle, idempotency, and migration proof."""
from __future__ import annotations

import sqlite3

from path_setup import ROOT  # noqa: F401 — make repository and src importable

from switchboard.domain.attention import AttentionLifecycleError
from switchboard.storage.migrations.attention import (
    downgrade_attention_schema,
    upgrade_attention_schema,
)
from switchboard.storage.repositories.attention import (
    AttentionStoreError,
    create_attention_request_in,
    record_attention_decision_in,
    reconstruct_attention_audit_in,
    transition_attention_request_in,
)

passed = failed = 0


def check(condition: bool, message: str) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {message}")
    else:
        failed += 1
        print(f"  FAIL  {message}")


def connection() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    upgrade_attention_schema(c)
    return c


def request_data(**overrides):
    data = {
        "request_id": "attention-1",
        "task_id": "PROTO-7",
        "provider": "openai",
        "host_id": "host-1",
        "runner_session_id": "runner-1",
        "work_session_id": "work-1",
        "provider_request_id": "provider-question-1",
        "schema_version": "provider.attention.v1",
        "prompt": "Choose a migration strategy",
        "context": {"frozen": True},
        "choices": [{"id": "safe"}, {"id": "fast"}],
        "recommended_default": {"id": "safe"},
        "expires_at": 100.0,
        "idempotency_key": "request-idem-1",
    }
    data.update(overrides)
    return data


# Reversible migration.
c = sqlite3.connect(":memory:")
c.row_factory = sqlite3.Row
upgrade_attention_schema(c)
upgrade_attention_schema(c)
tables = {row[0] for row in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
)}
check({"attention_requests", "attention_decisions", "attention_events"} <= tables,
      "upgrade creates all first-class attention tables idempotently")
downgrade_attention_schema(c)
tables = {row[0] for row in c.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
)}
check(not {"attention_requests", "attention_decisions", "attention_events"} & tables,
      "downgrade removes the attention schema in dependency order")

# Frozen request and idempotency.
c = connection()
first = create_attention_request_in(
    c, request_data(), project="switchboard", actor="host", now=10)
replay = create_attention_request_in(
    c, request_data(), project="switchboard", actor="host", now=20)
check(first["created"] is True and replay["idempotent_replay"] is True,
      "identical request retry returns the original durable request")
check(replay["request"]["context"] == {"frozen": True},
      "frozen provider context round-trips")
try:
    create_attention_request_in(
        c, request_data(prompt="mutated"), project="switchboard", actor="host", now=30)
except AttentionStoreError as exc:
    check(exc.code == "attention_idempotency_conflict",
          "same request idempotency key rejects different frozen content")
else:
    check(False, "same request idempotency key rejects different frozen content")

# Full lifecycle and audit reconstruction.
c = connection()
create_attention_request_in(
    c, request_data(), project="switchboard", actor="host", now=10)
decision = record_attention_decision_in(
    c, "attention-1",
    {"expected_version": 1, "choice": {"id": "safe"},
     "idempotency_key": "decision-idem-1"},
    actor="operator", actor_principal_id="principal-1", now=20)
check(decision["request"]["status"] == "decision_recorded"
      and decision["request"]["version"] == 2,
      "decision atomically advances the version-fenced request")
transition_attention_request_in(
    c, "attention-1", expected_version=2, target_status="delivering",
    actor="host", delivery_claimed_by="runner-1", now=30)
resolved = transition_attention_request_in(
    c, "attention-1", expected_version=3, target_status="resolved",
    actor="host", delivery_receipt={"provider_ack": "ack-1"}, now=40)
audit = reconstruct_attention_audit_in(c, "attention-1")
check(resolved["delivery_receipt"] == {"provider_ack": "ack-1"},
      "resolved request preserves the provider delivery receipt")
check([event["to_status"] for event in audit["events"]] == [
    "pending", "decision_recorded", "delivering", "resolved",
], "append-only events reconstruct the complete lifecycle")
check([event["request_version"] for event in audit["events"]] == [1, 2, 3, 4],
      "audit events preserve every optimistic-concurrency version")
check(audit["decisions"][0]["delivered_at"] == 40
      and audit["decisions"][0]["delivery_receipt"] == {"provider_ack": "ack-1"},
      "decision delivery metadata is reconstructable")

# Idempotent decision replay and stale writes.
replay = record_attention_decision_in(
    c, "attention-1",
    {"expected_version": 1, "choice": {"id": "safe"},
     "idempotency_key": "decision-idem-1"},
    actor="operator", actor_principal_id="principal-1", now=41)
check(replay["idempotent_replay"] is True,
      "identical decision retry returns the original decision after delivery")
try:
    record_attention_decision_in(
        c, "attention-1",
        {"expected_version": 1, "choice": {"id": "fast"},
         "idempotency_key": "decision-idem-2"},
        actor="operator", now=42)
except AttentionStoreError as exc:
    check(exc.code == "stale_attention_decision",
          "new decision against a stale request version is rejected")
else:
    check(False, "new decision against a stale request version is rejected")
try:
    transition_attention_request_in(
        c, "attention-1", expected_version=3,
        target_status="failed", actor="host", now=43)
except AttentionStoreError as exc:
    check(exc.code == "stale_attention_version",
          "stale lifecycle transition is rejected")
else:
    check(False, "stale lifecycle transition is rejected")

# Expiry and terminal state rules.
c = connection()
create_attention_request_in(
    c, request_data(), project="switchboard", actor="host", now=10)
try:
    transition_attention_request_in(
        c, "attention-1", expected_version=1,
        target_status="expired", actor="system", now=99)
except AttentionStoreError as exc:
    check(exc.code == "attention_not_expired",
          "expiry transition is rejected before expires_at")
else:
    check(False, "expiry transition is rejected before expires_at")
expired = transition_attention_request_in(
    c, "attention-1", expected_version=1,
    target_status="expired", actor="system", now=100)
check(expired["status"] == "expired", "pending request expires at its deadline")
try:
    transition_attention_request_in(
        c, "attention-1", expected_version=2,
        target_status="resolved", actor="system", now=101)
except AttentionLifecycleError:
    check(True, "terminal attention states reject further transitions")
else:
    check(False, "terminal attention states reject further transitions")

print(f"\nPROTO-7 attention store: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
