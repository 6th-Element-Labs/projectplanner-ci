#!/usr/bin/env python3
"""Closure verification is durable communication, never execution authority."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="closure-request-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deliverable_closure as closure  # noqa: E402
import store  # noqa: E402

PROJECT = "closure-request"
DELIVERABLE = "closure-request-deliverable"

store.init_project_registry()
store.create_project("Closure request", project_id=PROJECT, actor="test")
store.create_deliverable({
    "id": DELIVERABLE,
    "title": "Closure request",
    "status": "in_progress",
    "acceptance_criteria": ["verified"],
    "proof_requirements": {
        "schema": "switchboard.deliverable_proof_requirements.v1",
        "gates": [{"id": "scope", "required": True}],
    },
}, actor="test", project=PROJECT)

result = closure.request_closure_verification(
    DELIVERABLE, PROJECT, actor="operator")
assert result["requested"] is True
assert result["dispatched"] is False
assert result["execution_started"] is False
assert result.get("wake_id") is None
assert result["message_id"]

messages = store.list_agent_messages(
    agent=f"verifier/closure/{DELIVERABLE}", project=PROJECT)
assert len(messages) == 1
assert messages[0]["signal"] == closure.CLOSURE_VERIFICATION_SIGNAL
assert not store.list_wake_intents(project=PROJECT)

repeat = closure.request_closure_verification(
    DELIVERABLE, PROJECT, actor="operator")
assert repeat["message_id"] == result["message_id"]
assert len(store.list_agent_messages(
    agent=f"verifier/closure/{DELIVERABLE}", project=PROJECT)) == 1

print("closure verification mailbox boundary: PASS")
