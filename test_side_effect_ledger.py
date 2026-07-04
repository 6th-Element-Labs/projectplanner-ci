#!/usr/bin/env python3
"""HARDEN-21 external side-effect ledger regressions."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="side-effect-ledger-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    task = store.create_task({"workstream_id": "HARDEN", "title": "side effect test"},
                             actor="test", project=P)
    task_id = task["task_id"]

    claim = store.claim_external_effect(
        "github_write", "github", "repos/org/repo/statuses/abc",
        {"state": "pending", "context": "switchboard/vm-gate"},
        task_id=task_id, agent_id="codex/test", actor="codex/test", project=P)
    ok(claim["claimed"] is True and claim["effect"]["status"] == "claimed",
       "external effect can be claimed before provider write")
    duplicate_pending = store.claim_external_effect(
        "github_write", "github", "repos/org/repo/statuses/abc",
        {"context": "switchboard/vm-gate", "state": "pending"},
        task_id=task_id, agent_id="codex/test", actor="codex/test", project=P)
    ok(duplicate_pending["claimed"] is False and duplicate_pending["readback_required"] is True,
       "duplicate unverified effect requires readback before replay")
    store.mark_external_effect_issued(
        claim["effect_key"], {"provider_status": 201}, actor="codex/test", project=P)
    verified = store.verify_external_effect(
        claim["effect_key"], {"provider_status": 201, "sha": "abc"},
        actor="codex/test", project=P)
    ok(verified["effect"]["status"] == "verified" and verified["effect"]["readback"]["sha"] == "abc",
       "external effect confirms only after readback proof")
    duplicate_verified = store.claim_external_effect(
        "github_write", "github", "repos/org/repo/statuses/abc",
        {"state": "pending", "context": "switchboard/vm-gate"},
        task_id=task_id, agent_id="codex/test", actor="codex/test", project=P)
    ok(duplicate_verified["claimed"] is False and duplicate_verified["verified"] is True,
       "verified effect replay returns recorded proof")

    store.register_host(
        {
            "host_id": "host/wake",
            "runtimes": [{"runtime": "codex", "lanes": ["HARDEN"]}],
            "capacity": {"max_sessions": 2, "active_sessions": 0},
        },
        actor="host/wake", project=P)
    wake = store.request_wake(
        {"runtime": "codex", "lane": "HARDEN", "agent_id": "codex/test"},
        reason="wake once", source="codex/test", task_id=task_id,
        actor="codex/test", project=P)
    ok(wake.get("wake_id") and wake.get("effect_key"),
       "wake intent records a side-effect key")
    duplicate_wake = store.request_wake(
        {"agent_id": "codex/test", "lane": "HARDEN", "runtime": "codex"},
        reason="wake once", source="codex/test", task_id=task_id,
        actor="codex/test", project=P)
    ok(duplicate_wake["requested"] is False and duplicate_wake["readback_required"] is True,
       "duplicate wake does not create a second host intent while unverified")
    wakes = store.list_wake_intents(project=P)
    ok(len(wakes) == 1, "duplicate wake suppression leaves one wake row")
    completed_wake = store.complete_wake(
        wake["wake_id"], runner_session_id="run/wake", agent_id="codex/test",
        result={"started": True}, actor="host/wake", project=P)
    wake_effect = [e for e in store.list_external_effects(effect_type="wake", project=P)
                   if e["effect_key"] == wake["effect_key"]][0]
    ok(completed_wake["status"] == "completed" and wake_effect["status"] == "verified",
       "wake completion verifies the side effect")

    store.upsert_runner_session(
        {
            "runner_session_id": "run/control",
            "host_id": "host/wake",
            "agent_id": "codex/test",
            "runtime": "codex",
            "task_id": task_id,
            "status": "running",
            "control": {"managed_process": True, "runner_kill": True, "tier": "T3"},
        },
        actor="host/wake", project=P)
    kill = store.request_runner_control(
        "run/control", "kill", reason="stop once",
        options={"signal": "TERM", "grace_seconds": 1},
        actor="switchboard/operator", project=P)
    ok(kill["requested"] is True and kill.get("effect_key"),
       "runner control request records a side-effect key")
    duplicate_kill = store.request_runner_control(
        "run/control", "kill", reason="stop once",
        options={"signal": "TERM", "grace_seconds": 1},
        actor="switchboard/operator", project=P)
    ok(duplicate_kill["requested"] is False and duplicate_kill["readback_required"] is True,
       "duplicate runner control is blocked until host readback")
    controls = store.list_runner_control_requests(project=P, runner_session_id="run/control")
    ok(len(controls) == 1, "duplicate runner control suppression leaves one request row")
    complete_kill = store.complete_runner_control_request(
        kill["request_id"], result={"status": "killed"}, snapshot={"status": "killed"},
        actor="host/wake", project=P)
    kill_effect = [e for e in store.list_external_effects(effect_type="runner_control", project=P)
                   if e["effect_key"] == kill["effect_key"]][0]
    ok(complete_kill["status"] == "completed" and kill_effect["status"] == "verified",
       "runner control completion verifies provider/host readback")

    bundle = store.audit_export(project=P)
    ok(bundle["summary"]["side_effect_count"] >= 3 and "external_side_effects" in bundle,
       "audit export includes external side-effect ledger rows")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
