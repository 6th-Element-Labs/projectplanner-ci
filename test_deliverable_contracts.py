#!/usr/bin/env python3
"""DELIVERABLES-24 contract, revision, and lifecycle schema proof."""
import json
from pathlib import Path

from deliverable_contracts import (
    CONTRACT_SCHEMA, ContractError, ContractLedger, canonical_hash, canonical_json,
    derive_brief, legacy_baseline, material_changes, migrate_legacy_deliverable,
    validate_contract,
)

passed = failed = 0
def ok(value, message):
    global passed, failed
    print(("  PASS  " if value else "  FAIL  ") + message)
    passed += bool(value); failed += not bool(value)

def raises(code, fn):
    try: fn()
    except ContractError as exc: return exc.code == code
    return False

lite = {"schema": CONTRACT_SCHEMA, "contract_id": "d-1", "profile": "lite", "title": "Café",
        "outcome": "Ship value", "acceptance_criteria": ["proof exists"], "owner": "Product"}
reordered = {key: lite[key] for key in reversed(lite)}
ok(canonical_json(lite) == canonical_json(reordered), "canonical normalization ignores key order")
ok(canonical_hash(lite) == canonical_hash({**lite, "title": "Cafe\u0301"}), "Unicode-equivalent contracts hash identically")
ok(raises("unknown_normative_fields", lambda: validate_contract({**lite, "secret_policy": True})),
   "unknown normative fields fail closed")
ok(raises("profile_field_forbidden", lambda: validate_contract({**lite, "milestones": []})),
   "lite profile rejects full-only fields")

full = {**lite, "profile": "full", "milestones": [], "proof_requirements": ["test"],
        "why_it_matters": "customer trust"}
ok(material_changes(full, {**full, "title": "Editorial rename"}) == [], "title-only edit is non-material")
ok(material_changes(full, {**full, "outcome": "Different value"}) == ["outcome"], "outcome edit is material")

ledger = ContractLedger()
r1 = ledger.publish(full, actor="person/1", published_at=1)
r2 = ledger.publish({**full, "outcome": "Better value"}, actor="person/1", published_at=2)
ok(r1["contract"]["outcome"] == "Ship value" and r1["revision"] == 1 and r2["revision"] == 2,
   "published revisions are immutable snapshots")
ok(raises("stale_revision", lambda: ledger.decide("d-1", "approve_contract", expected_revision=1,
                                                  expected_hash=r1["contract_hash"], actor="person/2", decided_at=3)),
   "binding decisions reject stale revision and hash")
decision = ledger.decide("d-1", "accept", expected_revision=2, expected_hash=r2["contract_hash"],
                         actor="person/2", decided_at=3)
ok(decision["outcome"] == "accept", "exact-revision binding decision succeeds")
brief = derive_brief(r2)
ok(brief["source_hash"] == r2["contract_hash"] and "contract" not in brief, "brief is derived and non-normative")

legacy = {"id": "legacy-1", "title": "Legacy", "end_state": "Done",
          "acceptance_criteria": ["verified"], "owner_person_or_role": "Owner",
          "milestones": [], "proof_requirements": []}
migrated = migrate_legacy_deliverable(legacy)
ok(legacy_baseline(migrated) == legacy, "legacy baseline migration is reversible")

for name in (
    "switchboard.deliverable_contract.v1.json", "switchboard.deliverable_contract_revision.v1.json",
    "switchboard.deliverable_brief.v1.json", "switchboard.deliverable_review_room.v1.json",
    "switchboard.deliverable_review_participant.v1.json", "switchboard.deliverable_feedback.v1.json",
    "switchboard.deliverable_redline.v1.json", "switchboard.deliverable_contract_decision.v1.json",
    "switchboard.deliverable_waiver.v1.json", "switchboard.deliverable_acceptance_review.v1.json",
):
    schema = json.loads((Path("schemas") / name).read_text())
    ok(schema["$id"] == name[:-5] and schema.get("additionalProperties") is False,
       f"{name} is versioned and rejects unknown top-level fields")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
