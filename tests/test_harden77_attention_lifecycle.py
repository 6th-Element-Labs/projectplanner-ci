#!/usr/bin/env python3
"""HARDEN-77 adversarial attention lifecycle matrix."""
from __future__ import annotations

import sqlite3

from path_setup import ROOT  # noqa: F401
from switchboard.storage.migrations.attention import upgrade_attention_schema
from switchboard.storage.repositories.attention import (
    AttentionStoreError,
    claim_attention_decision_in,
    create_attention_request_in,
    record_attention_decision_in,
    reconcile_attention_lifecycle_in,
    reconstruct_attention_audit_in,
)

PROJECT = "switchboard"
HEAD = "a" * 40

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
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE task_git_state (task_id TEXT PRIMARY KEY, head_sha TEXT)")
    db.execute(
        "INSERT INTO task_git_state(task_id, head_sha) VALUES ('HARDEN-77', ?)",
        (HEAD,),
    )
    upgrade_attention_schema(db)
    return db


def request(db, *, suffix="1", now=100.0, expires_at=None):
    return create_attention_request_in(
        db,
        {
            "provider": "provider-a",
            "provider_request_id": f"native-{suffix}",
            "schema_version": "provider.attention.v1",
            "prompt": "Continue?",
            "choices": [{"id": "continue"}, {"id": "hold"}],
            "idempotency_key": f"request-{suffix}",
            "task_id": "HARDEN-77",
            "host_id": "host-a",
            "runner_session_id": "runner-a",
            "work_session_id": "work-a",
            "context": {"head_sha": HEAD},
            "expires_at": expires_at,
        },
        project=PROJECT,
        actor="host-a",
        now=now,
    )["request"]


def decide(db, item, *, suffix="1", now=101.0):
    return record_attention_decision_in(
        db,
        item["request_id"],
        {
            "expected_version": item["version"],
            "choice": {"id": "continue"},
            "idempotency_key": f"decision-{suffix}",
        },
        actor="operator",
        actor_principal_id="principal/operator",
        project=PROJECT,
        now=now,
    )


# Expiry is terminal, audited, and rejects late operator decisions.
db = connection()
item = request(db, expires_at=101.0)
try:
    decide(db, item, now=102.0)
except AttentionStoreError as exc:
    check(exc.code == "attention_request_expired",
          "late operator decision fails closed after expiry")
else:
    check(False, "late operator decision fails closed after expiry")
audit = reconstruct_attention_audit_in(db, item["request_id"], project=PROJECT)
check(audit["request"]["status"] == "expired"
      and audit["request"]["terminal_reason"] == "decision_rejected_after_expiry"
      and audit["events"][-1]["event_type"] == "attention.expired",
      "expiry is an audited terminal state")

# Exact-head binding cancels stale decisions without resuming wrong work.
db = connection()
item = request(db)
db.execute(
    "UPDATE task_git_state SET head_sha=? WHERE task_id='HARDEN-77'",
    ("b" * 40,),
)
try:
    decide(db, item)
except AttentionStoreError as exc:
    check(exc.code == "stale_attention_head",
          "head change rejects the stale decision")
else:
    check(False, "head change rejects the stale decision")
audit = reconstruct_attention_audit_in(db, item["request_id"], project=PROJECT)
check(audit["request"]["status"] == "cancelled"
      and audit["request"]["terminal_reason"] == "exact_head_binding_changed"
      and audit["decisions"] == [],
      "stale-head cancellation leaves no decision to deliver")

# Frozen choices forbid permission expansion via free-form custom answers.
db = connection()
item = request(db)
try:
    record_attention_decision_in(
        db,
        item["request_id"],
        {
            "expected_version": 1,
            "choice": {"id": "grant-admin"},
            "idempotency_key": "decision-expand",
        },
        actor="operator",
        actor_principal_id="principal/operator",
        project=PROJECT,
        now=101.0,
    )
except AttentionStoreError as exc:
    check(exc.code == "attention_choice_not_allowed",
          "decision must select one of the frozen request choices")
else:
    check(False, "decision must select one of the frozen request choices")

# Exact provider/runner/work-session binding is one-shot.
db = connection()
item = request(db)
decide(db, item)
wrong = claim_attention_decision_in(
    db, project=PROJECT, host_id="host-a", provider="provider-a",
    runner_session_id="runner-replaced", work_session_id="work-a",
    request_id=item["request_id"], actor="host-a", now=102.0)
claimed = claim_attention_decision_in(
    db, project=PROJECT, host_id="host-a", provider="provider-a",
    runner_session_id="runner-a", work_session_id="work-a",
    request_id=item["request_id"], actor="host-a", now=102.0)
duplicate = claim_attention_decision_in(
    db, project=PROJECT, host_id="host-a", provider="provider-a",
    runner_session_id="runner-a", work_session_id="work-a",
    request_id=item["request_id"], actor="host-a", now=102.0)
check(wrong is None and claimed["request"]["status"] == "delivering"
      and duplicate is None,
      "delivery claim binds exact runner/work session and is one-shot")

# Provider disconnect orphans the claim without redelivery.
db = connection()
item = request(db)
decide(db, item)
claim_attention_decision_in(
    db, project=PROJECT, host_id="host-a", provider="provider-a",
    runner_session_id="runner-a", work_session_id="work-a",
    request_id=item["request_id"], actor="host-a", now=102.0)
swept = reconcile_attention_lifecycle_in(
    db, project=PROJECT, now=403.0, delivery_timeout_s=300.0)
audit = reconstruct_attention_audit_in(db, item["request_id"], project=PROJECT)
redelivery = claim_attention_decision_in(
    db, project=PROJECT, host_id="host-a", provider="provider-a",
    runner_session_id="runner-a", work_session_id="work-a",
    request_id=item["request_id"], actor="host-a", now=404.0)
check(swept == {"expired": 0, "orphaned": 1}
      and audit["request"]["status"] == "orphaned"
      and audit["request"]["terminal_reason"] == "delivery_claim_timeout"
      and redelivery is None,
      "abandoned delivery orphans without redelivery")

# Reconcile materializes unanswered expiry.
db = connection()
item = request(db, expires_at=101.0)
swept = reconcile_attention_lifecycle_in(db, project=PROJECT, now=102.0)
audit = reconstruct_attention_audit_in(db, item["request_id"], project=PROJECT)
check(swept == {"expired": 1, "orphaned": 0}
      and audit["request"]["terminal_reason"] == "request_expiry_elapsed",
      "reconcile materializes unanswered expiry")

print(f"\nHARDEN-77 attention lifecycle: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
