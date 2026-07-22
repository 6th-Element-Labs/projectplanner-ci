#!/usr/bin/env python3
"""BUG-116: Start routes Triage BUGs before launching direct CLI work."""
from __future__ import annotations

import os
import shutil
import tempfile

from path_setup import ROOT  # noqa: F401

TMP = tempfile.mkdtemp(prefix="bug116-triage-start-")
os.environ["PM_DB_PATH"] = os.path.join(TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP

import dispatch  # noqa: E402
import store  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.application.commands import submit_bug  # noqa: E402

P = "qa-bug116"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


def submit(*, duplicate_of=""):
    return submit_bug.execute({
        "source_task": "FLOW-1",
        "source_agent": "agent/bug116-reporter",
        "observed_behavior": "Start launched a runner before intake routing",
        "expected_behavior": "Start routes the BUG before launching",
        "repro_steps": "Submit a BUG, then call start_task",
        "evidence": {"runner": "run_bug116"},
        "severity_hint": "critical",
        "affected_surface": "dispatch auth",
        "failure_class": "absent_permission",
        "duplicate_of": duplicate_of,
    }, actor="agent/bug116-reporter", project=P,
       # This proof exercises the explicit Start boundary itself. Production
       # submit_bug now calls that same boundary automatically, so keep this
       # fixture in Triage until the test invokes dispatch.start_task below.
       start_task=lambda *_args, **_kwargs: {
           "started": False, "action": "fixture_deferred",
       })


try:
    store.create_project("BUG-116 proof", project_id=P, actor="test")
    store.init_db(P)
    store.create_task(
        {"workstream_id": "FLOW", "title": "BUG-116 source"},
        actor="test", project=P)
    created = submit()
    bug_id = created["bug"]["task_id"]
    launch_observations = []
    original_dispatch = dispatch.dispatch

    def fake_dispatch(task_id, **kwargs):
        routed = store.get_task(task_id, project=P)
        report = (routed.get("agent_state") or {}).get("bug_report") or {}
        launch_observations.append({
            "status": routed.get("status"),
            "intake_status": report.get("intake_status"),
            "routing": report.get("routing"),
            "evidence": report.get("evidence"),
        })
        return {
            "dispatched": True,
            "wake_id": "wake-bug116",
            "host_id": "host/bug116",
            "branch": f"codex/{task_id.lower()}",
            "execution_mode": "direct_personal_cli",
        }

    dispatch.dispatch = fake_dispatch
    try:
        started = dispatch.start_task(
            bug_id, actor="operator/bug116", principal_id="user/bug116",
            project=P)
    finally:
        dispatch.dispatch = original_dispatch

    final = store.get_task(bug_id, project=P)
    report = (final.get("agent_state") or {}).get("bug_report") or {}
    routing = report.get("routing") or {}
    routed_events = [
        event for event in final.get("activity") or []
        if event.get("kind") == "bug.routed_for_implementation"
    ]
    ok(started.get("started") and started.get("intake_routing", {}).get("routed"),
       "Start returns the audited intake-routing receipt")
    ok(launch_observations and launch_observations[0]["status"] == "Not Started"
       and launch_observations[0]["intake_status"] == "routed",
       "the BUG is routed before the direct-session launcher runs")
    ok(final.get("status") == "Not Started"
       and routing.get("previous_status") == "Triage"
       and routing.get("next_status") == "Not Started",
       "routing advances the BUG into the ordinary claimable lifecycle")
    ok(routing.get("routed_by") == "operator/bug116"
       and routing.get("routed_principal_id") == "user/bug116"
       and routing.get("trigger") == "start_task" and routing.get("routed_at"),
       "routing records actor, principal, trigger, and time")
    ok(report.get("source_task") == "FLOW-1"
       and report.get("evidence") == {"runner": "run_bug116"},
       "routing preserves the original structured BUG report and evidence")
    ok(len(routed_events) == 1,
       "status transition and one dedicated routing audit event commit together")

    repeated = store.route_bug_for_implementation(
        bug_id, actor="operator/retry", principal_id="user/bug116", project=P)
    repeated_task = store.get_task(bug_id, project=P)
    repeated_events = [
        event for event in repeated_task.get("activity") or []
        if event.get("kind") == "bug.routed_for_implementation"
    ]
    ok(not repeated.get("routed") and repeated.get("ready")
       and len(repeated_events) == 1,
       "retries are idempotent and do not duplicate routing evidence")

    duplicate = submit(duplicate_of=bug_id)["bug"]
    called = []
    dispatch.dispatch = lambda *_args, **_kwargs: called.append(True) or {"dispatched": True}
    try:
        refused = dispatch.start_task(
            duplicate["task_id"], actor="operator/bug116",
            principal_id="user/bug116", project=P)
    finally:
        dispatch.dispatch = original_dispatch
    duplicate_after = store.get_task(duplicate["task_id"], project=P)
    ok(refused.get("action") == "refused"
       and refused.get("error") == "bug_intake_not_routable" and not called,
       "duplicate BUG intake is refused before any launcher side effect")
    ok(duplicate_after.get("status") == "Triage",
       "a refused duplicate remains visibly in Triage")

    legacy = store.create_task({
        "workstream_id": "BUG",
        "title": "Legacy task loses its stored defect context",
        "description": (
            "Starting this historical BUG fails because it predates structured intake."
        ),
        "status": "Triage",
        "risk_level": "High",
    }, actor="legacy-import", project=P)
    legacy_id = legacy["task_id"]
    routed_legacy = store.route_bug_for_implementation(
        legacy_id, actor="operator/bug116", principal_id="user/bug116", project=P)
    legacy_after = store.get_task(legacy_id, project=P)
    legacy_report = (legacy_after.get("agent_state") or {}).get("bug_report") or {}
    legacy_provenance = legacy_report.get("provenance") or {}
    ok(routed_legacy.get("routed") and legacy_after.get("status") == "Not Started",
       "Start normalizes and routes a legacy Triage BUG with a substantive description")
    ok(legacy_report.get("observed_behavior") == legacy_after.get("description")
       and legacy_report.get("expected_behavior") == ""
       and legacy_report.get("repro_steps") == ""
       and legacy_provenance.get("synthesized") is True
       and legacy_provenance.get("source") == "legacy_task_fields"
       and legacy_provenance.get("unavailable_fields")
       == ["expected_behavior", "repro_steps"]
       and routed_legacy.get("routing", {}).get("normalization") == legacy_provenance,
       "legacy normalization preserves known text without fabricating unknown fields")

    repeated_legacy = store.route_bug_for_implementation(
        legacy_id, actor="operator/retry", principal_id="user/bug116", project=P)
    repeated_legacy_after = store.get_task(legacy_id, project=P)
    repeated_legacy_events = [
        event for event in repeated_legacy_after.get("activity") or []
        if event.get("kind") == "bug.routed_for_implementation"
    ]
    ok(not repeated_legacy.get("routed") and repeated_legacy.get("ready")
       and len(repeated_legacy_events) == 1,
       "legacy normalization retries are idempotent")

    empty_legacy = store.create_task({
        "workstream_id": "BUG", "title": "Empty legacy BUG",
        "description": "", "status": "Triage",
    }, actor="legacy-import", project=P)
    refused_empty = store.route_bug_for_implementation(
        empty_legacy["task_id"], actor="operator/bug116", project=P)
    ok(not refused_empty.get("routed")
       and refused_empty.get("error") == "bug_intake_not_routable"
       and store.get_task(empty_legacy["task_id"], project=P).get("status") == "Triage",
       "empty legacy Triage BUGs still fail closed")

    malformed_legacy = store.create_task({
        "workstream_id": "BUG", "title": "Malformed legacy BUG",
        "description": "This description is substantive but its agent state is corrupt.",
        "status": "Triage",
    }, actor="legacy-import", project=P)
    with _conn(P) as conn:
        conn.execute(
            "UPDATE tasks SET agent_state=? WHERE task_id=?",
            ("{not-json", malformed_legacy["task_id"]),
        )
    refused_malformed = store.route_bug_for_implementation(
        malformed_legacy["task_id"], actor="operator/bug116", project=P)
    ok(not refused_malformed.get("routed")
       and refused_malformed.get("error") == "bug_intake_not_routable"
       and refused_malformed.get("reason") == "Triage BUG has malformed agent_state.",
       "malformed legacy agent state still fails closed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nBUG-116 triage Start conversion: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
