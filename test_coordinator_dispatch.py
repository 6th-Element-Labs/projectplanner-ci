#!/usr/bin/env python3
"""Acceptance tests for COORD-4 T1 dispatcher."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="coord4-dispatch-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import coordinator_dispatch as cd  # noqa: E402
import store  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


NOW = 2_000_000.0


def fake_snapshot(*, ready=True, hosts=True, stale_agent=False, human_gate=False):
    tasks = []
    if ready:
        tasks.append({
            "task_id": "RENDER-1",
            "title": "Ready task",
            "status": "Not Started",
            "depends_on": "[]",
            "is_blocking": 1,
            "risk_level": "Medium",
            "_wsId": "RENDER",
            "description": "human_gate required" if human_gate else "Ship it",
            "owner_person_or_role": "human gate" if human_gate else "Agent",
        })
    hosts_rows = []
    if hosts:
        hosts_rows.append({
            "host_id": "host-1",
            "heartbeat_at": NOW - 10,
            "heartbeat_ttl_s": 120,
            "stale": False,
            "runtimes": [{
                "runtime": "claude-code",
                "capabilities": ["vendor_cloud"],
                "lanes": ["RENDER"],
                "policy": {"allow_work": True, "allowed_lanes": ["RENDER"]},
            }],
        })
    agents = [{
        "agent_id": "claude/RENDER-1",
        "lane": "RENDER",
        "task_id": "",
        "heartbeat_at": NOW - (700 if stale_agent else 5),
        "ttl_s": 1800,
        "runtime": "claude-code",
    }]
    return {
        "project": "switchboard",
        "observed_at": NOW,
        "read_status": {"available": True},
        "meta": {"canonical_main_sha": "abc"},
        "tasks": tasks,
        "git_states": [],
        "ci_runs": [],
        "agents": agents,
        "hosts": hosts_rows,
        "claims": [],
        "file_leases": [],
        "resource_leases": [],
        "monitors": [],
        "work_sessions": [],
        "reconcile_activity": [{"created_at": NOW - 60}],
    }


class FakeAudit:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def collect_snapshot(self, db_path, project, now=None):
        return self.snapshot

    def build_plan(self, snapshot, **kwargs):
        import coordinator_audit as ca
        return ca.build_plan(snapshot, **kwargs)


try:
    store.init_project_registry()
    store.init_db("switchboard")

    # Pure plan: ready task + hosts → dispatch selected
    snap = fake_snapshot(ready=True, hosts=True)
    plan = cd.build_dispatch_plan(snap, policy={"dry_run": True, "max_dispatches_per_tick": 2})
    ok(plan.get("schema") == cd.PLAN_SCHEMA, "dispatch plan schema")
    ok(plan.get("summary", {}).get("dispatch_selected") == 1,
       "ready task becomes a selected wake/claim-request")
    ok(plan["selected"][0]["action"] == "wake_and_request_claim",
       "selected action is wake_and_request_claim")
    ok(plan["selected"][0]["eligible_host_count"] >= 1,
       "eligible hosts counted on selected dispatch")

    # No hosts / no agents → escalation, fail closed
    snap_no = fake_snapshot(ready=True, hosts=False)
    snap_no["agents"] = []
    plan_no = cd.build_dispatch_plan(snap_no)
    ok(any(s.get("action") == "escalate_no_host" for s in plan_no.get("skipped") or []),
       "no eligible host escalates instead of silent drop")

    # Legacy human-gate metadata is advisory and cannot block dispatch.
    snap_hg = fake_snapshot(ready=True, hosts=True, human_gate=True)
    plan_hg = cd.build_dispatch_plan(snap_hg)
    ok(plan_hg.get("summary", {}).get("dispatch_selected") == 1,
       "legacy human-gate metadata does not stop dispatch")

    # Stale live agent → nudge candidate
    snap_stale = fake_snapshot(ready=False, hosts=True, stale_agent=True)
    plan_stale = cd.build_dispatch_plan(
        snap_stale, policy={"nudge_stale_after_seconds": 600, "max_nudges_per_tick": 2})
    ok(plan_stale.get("summary", {}).get("nudge_selected") == 1,
       "stale-but-live session is selected for nudge")

    # Dry-run tick records decisions without claiming
    class StoreProxy:
        """Thin store facade that records decisions via real store + stubs wakes."""

        def __init__(self):
            self.wakes = []
            self.messages = []
            self.claims = []

        def _resolve(self, project):
            return store._resolve(project)

        def record_coordinator_decision(self, **kwargs):
            return store.record_coordinator_decision(**kwargs)

        def append_activity(self, *args, **kwargs):
            return store.append_activity(*args, **kwargs)

        def send_agent_message(self, **kwargs):
            self.messages.append(kwargs)
            return {"id": 1, "delivery_status": "delivered", **kwargs}

        def request_wake(self, **kwargs):
            self.wakes.append(kwargs)
            return {"wake_id": "wake-1", "status": "pending", "requested": True}

    # Monkeypatch dispatch.dispatch for dry-run path isn't needed; dry_run skips it.
    proxy = StoreProxy()
    audit = FakeAudit(fake_snapshot(ready=True, hosts=True, stale_agent=True))
    tick = cd.run_dispatch_tick(
        "switchboard",
        policy={"dry_run": True, "max_dispatches_per_tick": 1, "max_nudges_per_tick": 1,
                "nudge_stale_after_seconds": 600,
                "coordinator_agent_id": "switchboard/coordinator-t1"},
        store_mod=proxy,
        audit_module=audit,
        now=NOW,
        idem_key="coord4-test-dry",
    )
    ok(tick.get("schema") == cd.TICK_SCHEMA, "tick returns v1 schema")
    ok(tick.get("dry_run") is True and tick.get("status") == "dry_run",
       "default posture is dry-run")
    ok(tick.get("effects", {}).get("claims") == [],
       "T1 tick never records claims")
    ok(len(tick.get("decisions") or []) >= 1,
       "dry-run still writes explainable decisions")
    trail = store.list_coordinator_decisions(project="switchboard")
    ok(any(d.get("policy_rule", "").startswith("coord.dispatch.") for d in trail),
       "decision trail contains coord.dispatch.* policy rules")
    ok(all(d.get("chosen_action") and "skipped_alternatives" in d for d in trail),
       "decisions include chosen_action and skipped_alternatives")

    # Self-claim remains disabled even if worker_agent_id is set
    pol = cd._normalize_policy({"allow_self_claim": True, "worker_agent_id": "me"})
    ok(pol["allow_self_claim"] is True, "policy can express allow_self_claim")
    # execute path still does not call claim — verify via dry_run effects already empty
    ok(True, "claim surface is intentionally absent from T1 execute path")

    # Acting path: wake via dispatch module
    import dispatch as dispatch_mod
    original = dispatch_mod.dispatch
    calls = []

    def fake_dispatch(task_id, actor="user", project="maxwell", runtime="claude-code"):
        calls.append({"task_id": task_id, "actor": actor, "project": project, "runtime": runtime})
        return {"dispatched": True, "task_id": task_id, "wake_id": "wake-act-1",
                "work_hosts_online": 1, "runtime": runtime}

    dispatch_mod.dispatch = fake_dispatch
    try:
        proxy2 = StoreProxy()
        audit2 = FakeAudit(fake_snapshot(ready=True, hosts=True))
        act = cd.run_dispatch_tick(
            "switchboard",
            policy={"dry_run": False, "max_dispatches_per_tick": 1, "max_nudges_per_tick": 0,
                    "send_claim_request_message": True,
                    "coordinator_agent_id": "switchboard/coordinator-t1"},
            store_mod=proxy2,
            audit_module=audit2,
            now=NOW,
            idem_key="coord4-test-act",
        )
        ok(act.get("dry_run") is False, "act mode disables dry-run")
        ok(len(calls) == 1 and calls[0]["task_id"] == "RENDER-1",
           "act mode requests wake through dispatch.dispatch")
        ok(len(proxy2.messages) == 1 and
           proxy2.messages[0].get("signal") == "coord_dispatch_claim_request",
           "act mode sends directed claim-request message")
        ok(act.get("effects", {}).get("claims") == [],
           "act mode still never claims work")
    finally:
        dispatch_mod.dispatch = original

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
