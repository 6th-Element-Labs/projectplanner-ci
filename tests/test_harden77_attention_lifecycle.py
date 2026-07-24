"""HARDEN-77 adversarial attention lifecycle matrix."""
from __future__ import annotations

import sqlite3

import pytest

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


@pytest.fixture()
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE task_git_state (task_id TEXT PRIMARY KEY, head_sha TEXT)")
    connection.execute(
        "INSERT INTO task_git_state(task_id, head_sha) VALUES ('HARDEN-77', ?)",
        (HEAD,),
    )
    upgrade_attention_schema(connection)
    yield connection
    connection.close()


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


def test_expiry_is_terminal_audited_and_rejects_late_operator(db):
    item = request(db, expires_at=101.0)
    with pytest.raises(AttentionStoreError) as raised:
        decide(db, item, now=102.0)
    assert raised.value.code == "attention_request_expired"
    audit = reconstruct_attention_audit_in(db, item["request_id"], project=PROJECT)
    assert audit["request"]["status"] == "expired"
    assert audit["request"]["terminal_reason"] == "decision_rejected_after_expiry"
    assert audit["events"][-1]["event_type"] == "attention.expired"


def test_head_change_cancels_stale_decision_without_resuming_wrong_work(db):
    item = request(db)
    db.execute(
        "UPDATE task_git_state SET head_sha=? WHERE task_id='HARDEN-77'",
        ("b" * 40,),
    )
    with pytest.raises(AttentionStoreError) as raised:
        decide(db, item)
    assert raised.value.code == "stale_attention_head"
    audit = reconstruct_attention_audit_in(db, item["request_id"], project=PROJECT)
    assert audit["request"]["status"] == "cancelled"
    assert audit["request"]["terminal_reason"] == "exact_head_binding_changed"
    assert audit["decisions"] == []


def test_frozen_choices_forbid_permission_expansion(db):
    item = request(db)
    with pytest.raises(AttentionStoreError) as raised:
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
    assert raised.value.code == "attention_choice_not_allowed"


def test_exact_provider_runner_and_work_session_binding_is_one_shot(db):
    item = request(db)
    decide(db, item)
    wrong = claim_attention_decision_in(
        db, project=PROJECT, host_id="host-a", provider="provider-a",
        runner_session_id="runner-replaced", work_session_id="work-a",
        request_id=item["request_id"], actor="host-a", now=102.0)
    assert wrong is None
    claimed = claim_attention_decision_in(
        db, project=PROJECT, host_id="host-a", provider="provider-a",
        runner_session_id="runner-a", work_session_id="work-a",
        request_id=item["request_id"], actor="host-a", now=102.0)
    duplicate = claim_attention_decision_in(
        db, project=PROJECT, host_id="host-a", provider="provider-a",
        runner_session_id="runner-a", work_session_id="work-a",
        request_id=item["request_id"], actor="host-a", now=102.0)
    assert claimed["request"]["status"] == "delivering"
    assert duplicate is None


def test_provider_disconnect_orphans_claim_without_redelivery(db):
    item = request(db)
    decide(db, item)
    claimed = claim_attention_decision_in(
        db, project=PROJECT, host_id="host-a", provider="provider-a",
        runner_session_id="runner-a", work_session_id="work-a",
        request_id=item["request_id"], actor="host-a", now=102.0)
    swept = reconcile_attention_lifecycle_in(
        db, project=PROJECT, now=403.0, delivery_timeout_s=300.0)
    assert swept == {"expired": 0, "orphaned": 1}
    audit = reconstruct_attention_audit_in(db, item["request_id"], project=PROJECT)
    assert audit["request"]["status"] == "orphaned"
    assert audit["request"]["terminal_reason"] == "delivery_claim_timeout"
    assert claim_attention_decision_in(
        db, project=PROJECT, host_id="host-a", provider="provider-a",
        runner_session_id="runner-a", work_session_id="work-a",
        request_id=item["request_id"], actor="host-a", now=404.0) is None


def test_reconcile_materializes_unanswered_expiry(db):
    item = request(db, expires_at=101.0)
    swept = reconcile_attention_lifecycle_in(db, project=PROJECT, now=102.0)
    assert swept == {"expired": 1, "orphaned": 0}
    audit = reconstruct_attention_audit_in(db, item["request_id"], project=PROJECT)
    assert audit["request"]["terminal_reason"] == "request_expiry_elapsed"
