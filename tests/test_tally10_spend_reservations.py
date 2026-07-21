"""TALLY-10: atomic worst-case reservations and provider-actual reconciliation."""
import concurrent.futures
import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401
import scripts.switchboard_path  # noqa: F401,E402

tmp = tempfile.mkdtemp(prefix="tally10-")
os.environ["PM_DB_PATH"] = os.path.join(tmp, "project.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(tmp, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(tmp, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(tmp, "registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402

store.init_db()


def test_fail_closed_identity_cost_and_envelope():
    assert store.reserve_spend("", "missing-principal", 1)["failure_class"] == "unbound_identity"
    assert store.reserve_spend("principal/t10", "missing-cost", 0)["failure_class"] == "missing_data"
    assert store.reserve_spend("principal/t10", "missing-envelope", 1)["failure_class"] == "missing_data"


def test_atomic_concurrency_has_zero_overshoot():
    principal = "principal/concurrent"
    store.set_spend_envelope(principal, 1, 1)

    def attempt(number):
        return store.reserve_spend(principal, f"concurrent-{number}", "0.20")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(12)))
    accepted = [row for row in results if row.get("reservation_id")]
    rejected = [row for row in results if row.get("failure_class") == "budget_exceeded"]
    assert len(accepted) == 5
    assert len(rejected) == 7
    assert sum(row["reserved_micros"] for row in accepted) == 1_000_000


def test_retry_crash_and_reconciliation_are_idempotent():
    principal = "principal/retry"
    store.set_spend_envelope(principal, 3, 10)
    first = store.reserve_spend(principal, "provider-request-1", "2.50", {"attempt": 1})
    retry = store.reserve_spend(principal, "provider-request-1", "2.50", {"attempt": 2})
    assert retry["reservation_id"] == first["reservation_id"]
    assert retry["status"] == "reserved"  # crash before provider result preserves the debit

    actual = store.reconcile_spend(
        principal, "provider-request-1", "0.75", "openai", "gpt-test",
        prompt_tokens=100, completion_tokens=25)
    replay = store.reconcile_spend(
        principal, "provider-request-1", "0.75", "openai", "gpt-test",
        prompt_tokens=100, completion_tokens=25)
    assert replay["reservation_id"] == actual["reservation_id"]
    assert actual["status"] == "reconciled"
    assert actual["actual_micros"] == 750_000
    assert actual["provider"] == "openai" and actual["model"] == "gpt-test"
    assert actual["prompt_tokens"] == 100 and actual["completion_tokens"] == 25

    conflict = store.reconcile_spend(
        principal, "provider-request-1", "0.80", "openai", "gpt-test",
        prompt_tokens=100, completion_tokens=25)
    assert conflict["failure_class"] == "invalid_input"


if __name__ == "__main__":
    test_fail_closed_identity_cost_and_envelope()
    test_atomic_concurrency_has_zero_overshoot()
    test_retry_crash_and_reconciliation_are_idempotent()
    print("TALLY-10 spend reservation tests passed")
