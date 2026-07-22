"""Deliverable Contract / Review Room schemas (DELIVERABLES-24).

These are normative document contracts. Runtime validation and hashing live in
``deliverable_contracts``; this module registers the versioned JSON Schemas into
the ARCH-MS-42 registry so ``schemas/`` stays generated from live sources.
"""
from __future__ import annotations

from typing import Any

from ..registry import register_raw

CONTRACT_SCHEMA = "switchboard.deliverable_contract.v1"
REVISION_SCHEMA = "switchboard.deliverable_contract_revision.v1"
BRIEF_SCHEMA = "switchboard.deliverable_brief.v1"
DECISION_SCHEMA = "switchboard.deliverable_contract_decision.v1"
ROOM_SCHEMA = "switchboard.deliverable_review_room.v1"
PARTICIPANT_SCHEMA = "switchboard.deliverable_review_participant.v1"
FEEDBACK_SCHEMA = "switchboard.deliverable_feedback.v1"
REDLINE_SCHEMA = "switchboard.deliverable_redline.v1"
WAIVER_SCHEMA = "switchboard.deliverable_waiver.v1"
ACCEPTANCE_REVIEW_SCHEMA = "switchboard.deliverable_acceptance_review.v1"

_HASH_PATTERN = "^sha256:[0-9a-f]{64}$"

DELIVERABLE_SCHEMAS: dict[str, dict[str, Any]] = {
    CONTRACT_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Deliverable Contract",
        "description": "Sole normative authority for a deliverable; briefs are derived.",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "contract_id",
            "profile",
            "title",
            "outcome",
            "acceptance_criteria",
            "owner",
        ],
        "properties": {
            "schema": {"const": CONTRACT_SCHEMA},
            "contract_id": {"type": "string", "minLength": 1},
            "profile": {"enum": ["lite", "full"]},
            "title": {"type": "string", "minLength": 1},
            "outcome": {"type": "string", "minLength": 1},
            "acceptance_criteria": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "constraints": {"type": "array", "items": {"type": "string"}},
            "owner": {"type": "string", "minLength": 1},
            "proof_requirements": {"type": "array", "items": {"type": "string"}},
            "metadata": {"type": "object"},
            "why_it_matters": {"type": "string"},
            "milestones": {"type": "array", "items": {"type": "object"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "stakeholders": {"type": "array", "items": {"type": "string"}},
            "policy_constraints": {"type": "array", "items": {"type": "string"}},
        },
        "allOf": [
            {
                "if": {"properties": {"profile": {"const": "lite"}}},
                "then": {
                    "not": {
                        "anyOf": [
                            {"required": ["why_it_matters"]},
                            {"required": ["milestones"]},
                            {"required": ["risks"]},
                            {"required": ["stakeholders"]},
                            {"required": ["policy_constraints"]},
                        ]
                    }
                },
            },
            {
                "if": {"properties": {"profile": {"const": "full"}}},
                "then": {"required": ["milestones", "proof_requirements"]},
            },
        ],
    },
    REVISION_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Immutable Deliverable Contract Revision",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "contract_id",
            "revision",
            "contract_hash",
            "contract",
            "published_by",
            "published_at",
        ],
        "properties": {
            "schema": {"const": REVISION_SCHEMA},
            "contract_id": {"type": "string", "minLength": 1},
            "revision": {"type": "integer", "minimum": 1},
            "contract_hash": {"type": "string", "pattern": _HASH_PATTERN},
            "contract": {"$ref": CONTRACT_SCHEMA},
            "published_by": {"type": "string", "minLength": 1},
            "published_at": {"type": "number"},
        },
    },
    BRIEF_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Derived Deliverable Brief",
        "description": "Non-normative projection of one exact contract revision.",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "source_revision",
            "source_hash",
            "title",
            "outcome",
            "acceptance_criteria",
        ],
        "properties": {
            "schema": {"const": BRIEF_SCHEMA},
            "source_revision": {"type": "integer", "minimum": 1},
            "source_hash": {"type": "string", "pattern": _HASH_PATTERN},
            "title": {"type": "string"},
            "outcome": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "why_it_matters": {"type": ["string", "null"]},
        },
    },
    DECISION_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Exact-revision Deliverable Decision",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "contract_id",
            "outcome",
            "revision",
            "contract_hash",
            "decided_by",
            "decided_at",
        ],
        "properties": {
            "schema": {"const": DECISION_SCHEMA},
            "contract_id": {"type": "string", "minLength": 1},
            "outcome": {
                "enum": [
                    "approve_contract",
                    "request_changes",
                    "defer",
                    "no_go",
                    "accept",
                ]
            },
            "revision": {"type": "integer", "minimum": 1},
            "contract_hash": {"type": "string", "pattern": _HASH_PATTERN},
            "decided_by": {"type": "string", "minLength": 1},
            "decided_at": {"type": "number"},
        },
    },
    PARTICIPANT_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Review Room Participant",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "principal", "role"],
        "properties": {
            "schema": {"const": PARTICIPANT_SCHEMA},
            "principal": {"type": "string", "minLength": 1},
            "role": {"enum": ["owner", "reviewer", "approver", "observer"]},
        },
    },
    FEEDBACK_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Deliverable Feedback",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "feedback_id", "author", "body", "created_at"],
        "properties": {
            "schema": {"const": FEEDBACK_SCHEMA},
            "feedback_id": {"type": "string", "minLength": 1},
            "author": {"type": "string", "minLength": 1},
            "body": {"type": "string"},
            "created_at": {"type": "number"},
        },
    },
    REDLINE_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Deliverable Contract Redline",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "redline_id", "path", "before", "after", "author"],
        "properties": {
            "schema": {"const": REDLINE_SCHEMA},
            "redline_id": {"type": "string", "minLength": 1},
            "path": {"type": "string", "pattern": "^/"},
            "before": {},
            "after": {},
            "author": {"type": "string", "minLength": 1},
        },
    },
    WAIVER_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Deliverable Acceptance Waiver",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "waiver_id",
            "criterion",
            "reason",
            "approved_by",
            "approved_at",
        ],
        "properties": {
            "schema": {"const": WAIVER_SCHEMA},
            "waiver_id": {"type": "string", "minLength": 1},
            "criterion": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
            "approved_by": {"type": "string", "minLength": 1},
            "approved_at": {"type": "number"},
            "expires_at": {"type": ["number", "null"]},
        },
    },
    ACCEPTANCE_REVIEW_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Deliverable Acceptance Review",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "revision",
            "contract_hash",
            "evidence",
            "outcome",
            "reviewed_by",
            "reviewed_at",
        ],
        "properties": {
            "schema": {"const": ACCEPTANCE_REVIEW_SCHEMA},
            "revision": {"type": "integer", "minimum": 1},
            "contract_hash": {"type": "string", "pattern": _HASH_PATTERN},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "outcome": {
                "enum": ["accept", "request_changes", "defer", "no_go"]
            },
            "reviewed_by": {"type": "string", "minLength": 1},
            "reviewed_at": {"type": "number"},
        },
    },
    ROOM_SCHEMA: {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Deliverable Review Room",
        "description": (
            "Transport-neutral review aggregate for participants, feedback, "
            "redlines, waivers, decisions, and Acceptance Review."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "room_id",
            "contract_id",
            "contract_revision",
            "contract_hash",
            "contract_state",
            "delivery_state",
            "acceptance_state",
            "participants",
            "feedback",
            "redlines",
            "decisions",
            "waivers",
        ],
        "properties": {
            "schema": {"const": ROOM_SCHEMA},
            "room_id": {"type": "string", "minLength": 1},
            "contract_id": {"type": "string", "minLength": 1},
            "contract_revision": {"type": "integer", "minimum": 1},
            "contract_hash": {"type": "string", "pattern": _HASH_PATTERN},
            "contract_state": {
                "enum": [
                    "draft",
                    "proposed",
                    "approved",
                    "changes_requested",
                    "deferred",
                    "no_go",
                ]
            },
            "delivery_state": {
                "enum": [
                    "not_started",
                    "in_progress",
                    "in_review",
                    "done",
                    "blocked",
                ]
            },
            "acceptance_state": {
                "enum": [
                    "not_ready",
                    "ready",
                    "accepted",
                    "changes_requested",
                    "deferred",
                    "no_go",
                ]
            },
            "participants": {
                "type": "array",
                "items": {"$ref": "#/$defs/participant"},
            },
            "feedback": {"type": "array", "items": {"$ref": "#/$defs/feedback"}},
            "redlines": {"type": "array", "items": {"$ref": "#/$defs/redline"}},
            "decisions": {"type": "array", "items": {"$ref": "#/$defs/decision"}},
            "waivers": {"type": "array", "items": {"$ref": "#/$defs/waiver"}},
            "acceptance_review": {
                "anyOf": [
                    {"type": "null"},
                    {"$ref": "#/$defs/acceptance_review"},
                ]
            },
        },
        "$defs": {
            "participant": {
                "type": "object",
                "additionalProperties": False,
                "required": ["principal", "role"],
                "properties": {
                    "principal": {"type": "string"},
                    "role": {
                        "enum": ["owner", "reviewer", "approver", "observer"]
                    },
                },
            },
            "feedback": {
                "type": "object",
                "additionalProperties": False,
                "required": ["feedback_id", "author", "body", "created_at"],
                "properties": {
                    "feedback_id": {"type": "string"},
                    "author": {"type": "string"},
                    "body": {"type": "string"},
                    "created_at": {"type": "number"},
                },
            },
            "redline": {
                "type": "object",
                "additionalProperties": False,
                "required": ["redline_id", "path", "before", "after", "author"],
                "properties": {
                    "redline_id": {"type": "string"},
                    "path": {"type": "string"},
                    "before": {},
                    "after": {},
                    "author": {"type": "string"},
                },
            },
            "decision": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "outcome",
                    "expected_revision",
                    "expected_hash",
                    "decided_by",
                    "decided_at",
                ],
                "properties": {
                    "outcome": {
                        "enum": [
                            "approve_contract",
                            "request_changes",
                            "defer",
                            "no_go",
                            "accept",
                        ]
                    },
                    "expected_revision": {"type": "integer", "minimum": 1},
                    "expected_hash": {
                        "type": "string",
                        "pattern": _HASH_PATTERN,
                    },
                    "decided_by": {"type": "string"},
                    "decided_at": {"type": "number"},
                },
            },
            "waiver": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "waiver_id",
                    "criterion",
                    "reason",
                    "approved_by",
                    "approved_at",
                ],
                "properties": {
                    "waiver_id": {"type": "string"},
                    "criterion": {"type": "string"},
                    "reason": {"type": "string"},
                    "approved_by": {"type": "string"},
                    "approved_at": {"type": "number"},
                    "expires_at": {"type": ["number", "null"]},
                },
            },
            "acceptance_review": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "revision",
                    "contract_hash",
                    "evidence",
                    "outcome",
                    "reviewed_by",
                    "reviewed_at",
                ],
                "properties": {
                    "revision": {"type": "integer", "minimum": 1},
                    "contract_hash": {
                        "type": "string",
                        "pattern": _HASH_PATTERN,
                    },
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "outcome": {
                        "enum": [
                            "accept",
                            "request_changes",
                            "defer",
                            "no_go",
                        ]
                    },
                    "reviewed_by": {"type": "string"},
                    "reviewed_at": {"type": "number"},
                },
            },
        },
    },
}

for _schema_id, _payload in DELIVERABLE_SCHEMAS.items():
    register_raw(_schema_id, _payload)
