"""Transport-neutral Deliverable Contract and Review Room policy.

The normative contract is deliberately small and deterministic.  Presentation briefs
are derived from immutable revisions and never participate in policy decisions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

CONTRACT_SCHEMA = "switchboard.deliverable_contract.v1"
REVISION_SCHEMA = "switchboard.deliverable_contract_revision.v1"
ROOM_SCHEMA = "switchboard.deliverable_review_room.v1"

CONTRACT_LIFECYCLE = ("draft", "proposed", "approved", "changes_requested", "deferred", "no_go")
DELIVERY_LIFECYCLE = ("not_started", "in_progress", "in_review", "done", "blocked")
ACCEPTANCE_LIFECYCLE = ("not_ready", "ready", "accepted", "changes_requested", "deferred", "no_go")
BINDING_DECISIONS = ("approve_contract", "request_changes", "defer", "no_go", "accept")
PROFILES = ("lite", "full")

COMMON_FIELDS = {
    "schema", "contract_id", "profile", "title", "outcome", "acceptance_criteria",
    "constraints", "owner", "proof_requirements", "metadata",
}
FULL_ONLY_FIELDS = {"why_it_matters", "milestones", "risks", "stakeholders", "policy_constraints"}
NORMATIVE_FIELDS = COMMON_FIELDS | FULL_ONLY_FIELDS
REQUIRED = {
    "lite": {"schema", "contract_id", "profile", "title", "outcome", "acceptance_criteria", "owner"},
    "full": {"schema", "contract_id", "profile", "title", "outcome", "acceptance_criteria", "owner", "milestones", "proof_requirements"},
}
MATERIAL_FIELDS = {
    "profile", "outcome", "acceptance_criteria", "constraints", "owner", "proof_requirements",
    "milestones", "risks", "stakeholders", "policy_constraints",
}


class ContractError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.details = code, details


def _normalized(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("non_finite_number", "Canonical documents cannot contain NaN or infinity")
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ContractError("invalid_key", "Canonical document keys must be strings")
        normalized = {unicodedata.normalize("NFC", key): _normalized(item) for key, item in value.items()}
        if len(normalized) != len(value):
            raise ContractError("duplicate_normalized_key", "Keys collide after Unicode normalization")
        return {key: normalized[key] for key in sorted(normalized)}
    raise ContractError("unsupported_value", f"Unsupported canonical value: {type(value).__name__}")


def canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(_normalized(document), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_hash(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def validate_contract(document: Mapping[str, Any]) -> Dict[str, Any]:
    contract = copy.deepcopy(dict(document))
    unknown = sorted(set(contract) - NORMATIVE_FIELDS)
    if unknown:
        raise ContractError("unknown_normative_fields", "Unknown normative contract fields", fields=unknown)
    profile = contract.get("profile")
    if profile not in PROFILES:
        raise ContractError("invalid_profile", "Contract profile must be lite or full", profile=profile)
    if contract.get("schema") != CONTRACT_SCHEMA:
        raise ContractError("invalid_schema", "Contract schema is not supported", schema=contract.get("schema"))
    missing = sorted(REQUIRED[profile] - set(contract))
    if missing:
        raise ContractError("missing_fields", "Required contract fields are missing", fields=missing)
    if profile == "lite" and set(contract) & FULL_ONLY_FIELDS:
        raise ContractError("profile_field_forbidden", "Full-profile fields are forbidden in lite contracts",
                            fields=sorted(set(contract) & FULL_ONLY_FIELDS))
    if not isinstance(contract["acceptance_criteria"], list) or not contract["acceptance_criteria"]:
        raise ContractError("invalid_acceptance_criteria", "At least one acceptance criterion is required")
    return _normalized(contract)


def material_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    left, right = validate_contract(before), validate_contract(after)
    return sorted(field for field in MATERIAL_FIELDS if left.get(field) != right.get(field))


def derive_brief(revision: Mapping[str, Any]) -> Dict[str, Any]:
    contract = revision["contract"]
    return {
        "schema": "switchboard.deliverable_brief.v1",
        "source_revision": revision["revision"],
        "source_hash": revision["contract_hash"],
        "title": contract["title"],
        "outcome": contract["outcome"],
        "acceptance_criteria": copy.deepcopy(contract["acceptance_criteria"]),
        "why_it_matters": contract.get("why_it_matters"),
    }


@dataclass
class ContractLedger:
    """Minimal immutable revision and exact-revision decision aggregate."""
    revisions: Dict[str, list[Dict[str, Any]]] = field(default_factory=dict)
    decisions: list[Dict[str, Any]] = field(default_factory=list)

    def publish(self, document: Mapping[str, Any], *, actor: str, published_at: float) -> Dict[str, Any]:
        contract = validate_contract(document)
        history = self.revisions.setdefault(contract["contract_id"], [])
        revision = {
            "schema": REVISION_SCHEMA,
            "contract_id": contract["contract_id"],
            "revision": len(history) + 1,
            "contract_hash": canonical_hash(contract),
            "contract": contract,
            "published_by": actor,
            "published_at": published_at,
        }
        history.append(copy.deepcopy(revision))
        return copy.deepcopy(revision)

    def latest(self, contract_id: str) -> Dict[str, Any]:
        try:
            return copy.deepcopy(self.revisions[contract_id][-1])
        except (KeyError, IndexError) as exc:
            raise ContractError("contract_not_published", "Contract has no published revision") from exc

    def decide(self, contract_id: str, outcome: str, *, expected_revision: int,
               expected_hash: str, actor: str, decided_at: float) -> Dict[str, Any]:
        if outcome not in BINDING_DECISIONS:
            raise ContractError("invalid_decision", "Unsupported binding decision", outcome=outcome)
        current = self.latest(contract_id)
        if current["revision"] != expected_revision or current["contract_hash"] != expected_hash:
            raise ContractError("stale_revision", "Decision target is not the latest exact revision",
                                expected_revision=expected_revision, expected_hash=expected_hash,
                                actual_revision=current["revision"], actual_hash=current["contract_hash"])
        decision = {
            "schema": "switchboard.deliverable_contract_decision.v1", "contract_id": contract_id,
            "outcome": outcome, "revision": expected_revision, "contract_hash": expected_hash,
            "decided_by": actor, "decided_at": decided_at,
        }
        self.decisions.append(copy.deepcopy(decision))
        return decision


LEGACY_MAP = {
    "id": "contract_id", "title": "title", "end_state": "outcome",
    "acceptance_criteria": "acceptance_criteria", "owner_person_or_role": "owner",
    "why_it_matters": "why_it_matters", "policy_constraints": "policy_constraints",
    "proof_requirements": "proof_requirements", "milestones": "milestones",
}


def migrate_legacy_deliverable(legacy: Mapping[str, Any], profile: str = "full") -> Dict[str, Any]:
    contract = {new: copy.deepcopy(legacy[old]) for old, new in LEGACY_MAP.items() if old in legacy}
    contract.update({"schema": CONTRACT_SCHEMA, "profile": profile})
    if "acceptance_criteria" in contract and isinstance(contract["acceptance_criteria"], str):
        contract["acceptance_criteria"] = [contract["acceptance_criteria"]]
    return validate_contract(contract)


def legacy_baseline(contract: Mapping[str, Any]) -> Dict[str, Any]:
    valid = validate_contract(contract)
    inverse = {new: old for old, new in LEGACY_MAP.items()}
    return {inverse[key]: copy.deepcopy(value) for key, value in valid.items() if key in inverse}
