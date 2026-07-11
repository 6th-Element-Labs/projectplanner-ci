#!/usr/bin/env python3
"""NARRATE-7: executable narration_requested contract and negative fixtures."""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import narration_events as contract


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def error_code(fn):
    try:
        fn()
    except contract.NarrationEventValidationError as exc:
        return exc.code
    return None


fixture_dir = Path(__file__).with_name("fixtures") / "narration_events"
for fixture_path in sorted(fixture_dir.glob("*.json")):
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    expected = fixture["expected"]
    context = fixture.get("validation_context") or {}
    if expected == "valid":
        result = contract.validate_narration_requested(fixture["event"], **context)
        ok(result["event_id"] == fixture["event"]["event_id"],
           f"{fixture_path.name} is accepted")
    else:
        actual = error_code(
            lambda f=fixture, c=context: contract.validate_narration_requested(f["event"], **c)
        )
        ok(actual == expected,
           f"{fixture_path.name} rejects with {expected} (got {actual})")

valid_fixture = json.loads((fixture_dir / "valid_task.json").read_text(encoding="utf-8"))
valid = valid_fixture["event"]

# Canonical JSON hashing is stable across mapping order and feeds the event fingerprint.
source_a = {"status": "In Review", "title": "Ship narration events", "updated_at": 1780000000}
source_b = {"updated_at": 1780000000, "title": "Ship narration events", "status": "In Review"}
ok(contract.canonical_source_hash(source_a) == valid["source_hash"] and
   contract.canonical_source_hash(source_b) == valid["source_hash"],
   "canonical source hash is order-independent")

# Builder emits the same strict shape and initial attempt state producers must persist.
built = contract.build_narration_requested(
    event_id="nr-event-00000010",
    project="switchboard",
    entity_type="deliverable",
    entity_id="deliverable-event-driven-llm-narration",
    source_revision=3,
    source_hash=contract.canonical_source_hash({"status": "in_progress", "linked": 4}),
    causal_event={
        "event_id": "deliverable-change-3",
        "kind": "deliverable.material_changed",
        "occurred_at": 1780000000,
        "actor_id": "switchboard-system",
    },
    requested_at=1780000001,
    authorization={
        "principal_id": "principal-switchboard-system",
        "decision_id": "authz-00000010",
        "scope": "narration:request",
        "project": "switchboard",
    },
    trace_id="trace-00000010",
)
ok(built["entity_type"] == "deliverable" and built["attempt"]["state"] == "pending" and
   built["attempt"]["count"] == 0,
   "builder emits a validated pending deliverable request")

tampered = deepcopy(valid)
tampered["entity_id"] = "NARRATE-8"
ok(error_code(lambda: contract.validate_narration_requested(
    tampered, expected_project="switchboard", now=1780000010)) == "dedupe_mismatch",
   "dedupe key is bound to immutable entity and source fields")

auth_cross_project = deepcopy(valid)
auth_cross_project["authorization"]["project"] = "helm"
ok(error_code(lambda: contract.validate_narration_requested(
    auth_cross_project, expected_project="switchboard", now=1780000010)) == "cross_project",
   "authorization receipt cannot cross a project boundary")

same_revision_different_hash = deepcopy(valid)
ok(error_code(lambda: contract.validate_narration_requested(
    same_revision_different_hash,
    expected_project="switchboard",
    current_source_revision=7,
    current_source_hash="sha256:" + ("0" * 64),
    now=1780000010,
)) == "revision_collision",
   "equal revisions with different hashes fail closed")

bad_supersedes = deepcopy(valid)
bad_supersedes["supersedes"] = {"event_id": "nr-event-older", "source_revision": 7}
ok(error_code(lambda: contract.validate_narration_requested(
    bad_supersedes, expected_project="switchboard", now=1780000010)) == "revision_regression",
   "supersedes relation must point to an older revision")

claimed_without_lease = deepcopy(valid)
claimed_without_lease["attempt"]["state"] = "claimed"
claimed_without_lease["attempt"]["count"] = 1
ok(error_code(lambda: contract.validate_narration_requested(
    claimed_without_lease, expected_project="switchboard", now=1780000010)) == "malformed_event",
   "claimed attempt requires a worker identity and bounded lease")

unknown = deepcopy(valid)
unknown["provider_model"] = "should-be-a-receipt-not-a-request-field"
ok(error_code(lambda: contract.validate_narration_requested(
    unknown, expected_project="switchboard", now=1780000010)) == "unknown_field",
   "unknown v1 fields fail closed instead of being silently ignored")

ok(contract.request_disposition(
    valid, expected_project="switchboard", current_source_revision=8,
    current_source_hash=valid["source_hash"],
) == "stale", "older queued revision is classified stale before provider work")
ok(contract.request_disposition(
    valid, expected_project="switchboard", current_source_revision=7,
    current_source_hash=valid["source_hash"],
) == "current", "matching revision and hash is eligible for provider work")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
