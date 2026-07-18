#!/usr/bin/env python3
"""UI-39: end-to-end regressions for reliable Autopilot dispatch plumbing."""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import urllib.error
from pathlib import Path

from path_setup import ROOT  # noqa: F401


TMP = Path(tempfile.mkdtemp(prefix="ui39-runtime-closure-"))
os.environ["PM_DB_PATH"] = str(TMP / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(TMP / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(TMP / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(TMP / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(TMP)
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from adapters import agent_host  # noqa: E402
from adapters import switchboard_core  # noqa: E402
from db.connection import _conn  # noqa: E402
from switchboard.storage.repositories.deliverables import _mission_next_actions  # noqa: E402


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


try:
    store.init_db(P)

    # A periodic registration must never overwrite the accurate heartbeat with
    # the zero-active snapshot created when the daemon process first started.
    inventory = {
        "host_id": "host/ui39-capacity",
        "limits": {"max_sessions": 8},
        "policy": {"allow_work": True},
        "capacity": {"active_sessions": 0, "headroom": 8,
                     "placement": {"drain_state": "accepting"}},
    }
    real_active_count = agent_host.active_session_count
    agent_host.active_session_count = lambda _inventory: 3
    try:
        advertised = agent_host.registration_inventory(inventory)
        draining = agent_host.registration_inventory(inventory, drain_request={"reason": "test"})
    finally:
        agent_host.active_session_count = real_active_count
    ok(advertised["capacity"]["active_sessions"] == 3
       and advertised["capacity"]["headroom"] == 5,
       "periodic registration advertises live supervisor usage and headroom")
    ok(draining["capacity"]["active_sessions"] == 3
       and draining["capacity"]["placement"]["drain_state"] == "draining",
       "live registration capacity survives drain advertisement")

    # Preserve a stable enrolled principal when the display-name actor completes
    # a wake, then prove the enrolled principal can still heartbeat the row.
    task = store.create_task({
        "workstream_id": "UI", "title": "UI-39 principal preservation",
        "status": "Not Started", "ui_impact": "no",
    }, actor="ui39-test", project=P)
    task_id = task["task_id"]
    host_id = "host/ui39-mac"
    principal_id = "principal/ui39-enrolled"
    runner_id = "run_ui39_principal"
    store.register_host({
        "host_id": host_id,
        "runtimes": [{"runtime": "codex", "lanes": ["UI"]}],
        "limits": {"max_sessions": 8},
        "capacity": {"active_sessions": 0, "headroom": 8},
        "heartbeat_ttl_s": 60,
    }, principal_id=principal_id, actor=host_id, project=P)
    wake = store.request_wake(
        {"runtime": "codex", "lane": "UI", "agent_id": f"codex/{task_id}"},
        reason="UI-39 regression", source="ui39-test", task_id=task_id,
        actor="ui39-test", project=P)
    claimed_wake = store.claim_wake(host_id, wake["wake_id"],
                                    actor=host_id, project=P)
    ok(claimed_wake.get("claimed") is True, "enrolled host claims the exact wake")
    store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id,
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "status": "starting", "metadata": {"wake_id": wake["wake_id"]},
    }, principal_id=principal_id, actor=host_id, project=P)
    store.complete_wake(
        wake["wake_id"], runner_session_id=runner_id,
        agent_id=f"codex/{task_id}", result={"started": True, "pid": 39001},
        actor=host_id, project=P)
    completed_runner = store.get_runner_session(runner_id, project=P)
    renewed_runner = store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": host_id,
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "status": "running", "metadata": {"wake_id": wake["wake_id"]},
    }, principal_id=principal_id, actor=host_id, project=P)
    ok(completed_runner.get("principal_id") == principal_id,
       "wake completion cannot replace stable principal with host display name")
    ok(not renewed_runner.get("error") and renewed_runner.get("principal_id") == principal_id,
       "enrolled principal can heartbeat the completed runner without a 403 identity conflict")

    # Legacy board states must translate into real, non-looping dispatch behavior.
    ready_actions = _mission_next_actions({}, [{
        "project_id": P, "task_id": "DOCS-1", "blocks_deliverable": True,
        "role": "implementation", "task_detail": {
            "task_id": "DOCS-1", "title": "Legacy ready", "status": "Ready",
            "active_claims": [], "dependency_state": {"ready": True},
        },
    }], None)
    ok(any(row.get("action") == "claim_task" for row in ready_actions),
       "legacy Ready status becomes a concrete automatic claim action")

    orphan = store.create_task({
        "workstream_id": "UI", "title": "UI-39 orphan adoption",
        "status": "Not Started", "ui_impact": "no",
    }, actor="ui39-test", project=P)
    orphan_id = orphan["task_id"]
    orphan_agent = f"codex/{orphan_id}"
    orphan_principal = "principal/ui39-orphan"
    with _conn(P) as conn:
        conn.execute("UPDATE tasks SET status='In Progress' WHERE task_id=?", (orphan_id,))
    no_session = store.claim_task(
        orphan_id, orphan_agent, principal_id=orphan_principal,
        actor="ui39-test", project=P)
    ok(no_session.get("reason") == "orphan_work_session_required",
       "orphan adoption fails closed without a Work Session")
    session = store.create_work_session({
        "task_id": orphan_id, "agent_id": orphan_agent, "runtime": "codex",
        "repo_role": "canonical", "branch": f"codex/{orphan_id}-adopt",
        "upstream": "origin/master", "base_sha": "a" * 40, "head_sha": "a" * 40,
        "worktree_path": str(ROOT), "storage_mode": "worktree", "status": "active",
        "dirty_status": "clean", "conflict_marker_count": 0,
        "policy_profile": "code_strict",
        "hygiene": {"repo_preflight": {"ok": True, "verdict": "pass", "findings": []}},
    }, actor="ui39-test", principal_id=orphan_principal, project=P)
    session_id = session["work_session"]["work_session_id"]
    adopted = store.claim_task(
        orphan_id, orphan_agent, principal_id=orphan_principal,
        actor="ui39-test", work_session_id=session_id,
        session_policy_profile="code_strict", require_work_session=True, project=P)
    second_adoption = store.claim_task(
        orphan_id, "codex/other", principal_id="principal/other",
        actor="ui39-test", work_session_id=session_id,
        session_policy_profile="code_strict", require_work_session=True, project=P)
    with _conn(P) as conn:
        adopted_event = conn.execute(
            "SELECT 1 FROM activity WHERE task_id=? AND kind='task.orphan_claim_adopted'",
            (orphan_id,),
        ).fetchone()
    ok(adopted.get("claimed") is True
       and adopted.get("dispatch_reason", {}).get("orphan_adopted") is True
       and adopted_event is not None,
       "one Work-Session-bound claim safely adopts orphaned In Progress work")
    ok(second_adoption.get("reason") == "active_claim",
       "a live adopted claim prevents a second worker takeover")

    # Worker errors must expose the authoritative server reason, but not arbitrary
    # response fields or query strings.
    real_urlopen = switchboard_core.urllib.request.urlopen
    error_body = json.dumps({
        "detail": "host bearer cannot replace another runner identity",
        "secret": "must-not-leak",
    }).encode()

    def denied(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://plan.example/ixp/v1/heartbeat?token=hidden", 403,
            "Forbidden", {}, io.BytesIO(error_body))

    switchboard_core.urllib.request.urlopen = denied
    try:
        switchboard_core._http(
            "POST", "/ixp/v1/heartbeat?token=hidden", {}, base="https://plan.example")
        denial = ""
    except RuntimeError as exc:
        denial = str(exc)
    finally:
        switchboard_core.urllib.request.urlopen = real_urlopen
    ok("HTTP 403 /ixp/v1/heartbeat" in denial
       and "host bearer cannot replace another runner identity" in denial,
       "worker preserves the exact authoritative HTTP denial reason")
    ok("must-not-leak" not in denial and "token=hidden" not in denial,
       "worker error redaction excludes arbitrary response fields and query secrets")
finally:
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\nUI-39 Autopilot runtime closure: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
