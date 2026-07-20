#!/usr/bin/env python3
"""SIMPLIFY-2: active Autopilot scope survives deliverable board surgery."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


tmp = tempfile.mkdtemp(prefix="simplify2-scope-")
os.environ["PM_DB_PATH"] = str(Path(tmp) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(tmp) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(tmp) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(tmp) / "registry.db")

import store  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def deliverable(deliverable_id: str):
    return store.create_deliverable(
        {"id": deliverable_id, "title": deliverable_id, "status": "approved",
         "end_state": "Scope remains continuous."},
        actor="fixture", project="switchboard")


try:
    store.init_db("switchboard")
    for deliverable_id in (
            "scope-source", "scope-replacement", "scope-stop",
            "scope-conflict-source", "scope-conflict-target", "scope-no-source"):
        deliverable(deliverable_id)

    original = store.start_autopilot_scope(
        project="switchboard", deliverable_id="scope-source",
        scope_type="deliverable", actor="operator")
    store.update_autopilot_scope(
        original["scope_id"], project="switchboard",
        last_result={"decision_stream": [{"task_id": "SEG-5", "action": "ensure"}]})
    replacement = store.update_deliverable(
        "scope-source",
        {"status": "archived", "replacement_deliverable_id": "scope-replacement",
         "scope_transition_reason": "board surgery"},
        actor="codex/SIMPLIFY-2", project="switchboard")
    transition = replacement.get("autopilot_scope_transition") or {}
    moved = store.get_autopilot_scope(original["scope_id"], project="switchboard")
    ok(transition.get("action") == "transferred"
       and transition.get("scope_ids") == [original["scope_id"]],
       "replacement reports one explicit scope transfer")
    ok(moved.get("scope_id") == original["scope_id"]
       and moved.get("deliverable_id") == "scope-replacement"
       and moved.get("status") == "active" and moved.get("generation") == 2,
       "transfer preserves the active scope identity and increments its generation")
    ok(moved.get("last_result", {}).get("decision_stream")
       == [{"task_id": "SEG-5", "action": "ensure"}],
       "transfer preserves the existing lifecycle decision stream")
    ok(moved.get("last_result", {}).get("scope_transition", {}).get("reason")
       == "board surgery",
       "transfer appends its actor, reason, and target to durable scope history")
    repeated = store.start_autopilot_scope(
        project="switchboard", deliverable_id="scope-replacement",
        scope_type="deliverable", actor="operator")
    ok(repeated.get("scope_id") == original["scope_id"]
       and repeated.get("already_started") is True,
       "starting the replacement cannot fork a second execution scope")
    ok(replacement.get("metadata", {}).get("replacement_deliverable_id")
       == "scope-replacement",
       "the replaced deliverable retains a durable replacement pointer")
    no_source_scope = store.update_deliverable(
        "scope-no-source",
        {"status": "archived", "replacement_deliverable_id": "scope-replacement"},
        actor="codex/SIMPLIFY-2", project="switchboard")
    ok(no_source_scope.get("status") == "archived"
       and no_source_scope.get("autopilot_scope_transition", {}).get("action")
       == "no_live_scope",
       "board surgery without a source scope does not conflict with the live target")

    stopped_scope = store.start_autopilot_scope(
        project="switchboard", deliverable_id="scope-stop",
        scope_type="deliverable", actor="operator")
    archived = store.archive_deliverable(
        "scope-stop", project="switchboard", actor="codex/SIMPLIFY-2",
        archived=True, scope_transition_reason="operator retired the outcome")
    stopped = store.get_autopilot_scope(stopped_scope["scope_id"], project="switchboard")
    stop_transition = archived.get("autopilot_scope_transition") or {}
    ok(stopped.get("status") == "stopped" and stopped.get("generation") == 2,
       "archiving without a replacement explicitly stops the active scope")
    with store._conn("switchboard") as connection:
        message = connection.execute(
            "SELECT * FROM agent_messages WHERE id=?",
            (stop_transition.get("operator_message_id"),)).fetchone()
        event = connection.execute(
            "SELECT actor,payload FROM activity WHERE kind='autopilot.scope_stopped' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    event_payload = json.loads(event["payload"] if event else "{}")
    ok(message and message["to_agent"] == "switchboard/operator"
       and message["requires_ack"] == 1
       and "operator retired the outcome" in message["message"],
       "explicit stop creates a visible acknowledged operator notification")
    ok(event and event["actor"] == "codex/SIMPLIFY-2"
       and event_payload.get("operator_message_id") == message["id"],
       "scope stop and its operator notification share one audited transaction")

    source_scope = store.start_autopilot_scope(
        project="switchboard", deliverable_id="scope-conflict-source",
        scope_type="deliverable", actor="operator")
    target_scope = store.start_autopilot_scope(
        project="switchboard", deliverable_id="scope-conflict-target",
        scope_type="deliverable", actor="operator")
    conflict = store.update_deliverable(
        "scope-conflict-source",
        {"status": "archived",
         "replacement_deliverable_id": "scope-conflict-target"},
        actor="codex/SIMPLIFY-2", project="switchboard")
    source_after = store.get_autopilot_scope(source_scope["scope_id"], project="switchboard")
    target_after = store.get_autopilot_scope(target_scope["scope_id"], project="switchboard")
    deliverable_after = store.get_deliverable(
        "scope-conflict-source", project="switchboard")
    ok(conflict.get("error")
       == "replacement deliverable already has a live autopilot scope",
       "a replacement that already owns a live stream fails closed")
    ok(source_after.get("status") == "active"
       and target_after.get("status") == "active"
       and deliverable_after.get("status") == "approved",
       "a rejected transfer leaves both scopes and the deliverable unchanged")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\nSIMPLIFY-2 scope continuity: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
