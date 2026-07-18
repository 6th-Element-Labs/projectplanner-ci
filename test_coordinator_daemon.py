#!/usr/bin/env python3
"""COORD-8 acceptance tests for the durable autopilot daemon shell."""
from __future__ import annotations

from pathlib import Path
import sys

import coordinator_daemon as daemon_mod

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


class FakeStore:
    def __init__(self, clock):
        self.clock = clock
        self.meta = {}
        self.presence = {}
        self.leases = {}
        self.effects = {}
        self.calls = []
        self.cursor = 0
        self.wakes = []

    def get_meta(self, key, default=None, project="switchboard"):
        value = self.meta.get((project, key), default)
        return dict(value) if isinstance(value, dict) else value

    def set_meta(self, key, value, project="switchboard"):
        self.meta[(project, key)] = dict(value)

    def append_activity(self, kind, actor, payload, project="switchboard", **_kwargs):
        self.cursor += 1
        self.calls.append((kind, project, payload))
        return self.cursor

    def _activity_cursor(self, project):
        return self.cursor

    def heartbeat(self, agent_id, project="switchboard", actor="system"):
        if agent_id not in self.presence:
            return {"error": "agent not registered"}
        self.presence[agent_id]["heartbeat_at"] = self.clock()
        return dict(self.presence[agent_id])

    def register_agent(self, agent_id, runtime, **kwargs):
        row = {"agent_id": agent_id, "runtime": runtime,
               "heartbeat_at": self.clock(), **kwargs}
        self.presence[agent_id] = row
        return dict(row)

    def claim_resources(self, agent_id, resource_type, names, ttl_seconds=120,
                        project="switchboard", **_kwargs):
        name = names[0]
        for lease in self.leases.values():
            if (lease["project"] == project and lease["name"] == name
                    and lease["expires_at"] > self.clock()
                    and lease["agent_id"] != agent_id
                    and not lease.get("released")):
                return {"conflict": lease["agent_id"],
                        "retry_after_seconds": 5}
        lease_id = f"lease-{len(self.leases) + 1}"
        row = {"lease_id": lease_id, "agent_id": agent_id, "project": project,
               "name": name, "expires_at": self.clock() + ttl_seconds}
        self.leases[lease_id] = row
        return dict(row)

    def release_resource_lease(self, lease_id, **_kwargs):
        if lease_id in self.leases:
            self.leases[lease_id]["released"] = True
        return {"released": True, "lease_id": lease_id}

    def list_deliverables(self, **_kwargs):
        return [{"id": "deliverable-a", "status": "approved"},
                {"id": "deliverable-b", "status": "in_review"}]

    def list_autopilot_scopes(self, **_kwargs):
        return [
            {"scope_id": "scope-a", "scope_type": "deliverable",
             "deliverable_id": "deliverable-a", "runtime": "codex", "status": "active"},
            {"scope_id": "scope-b", "scope_type": "deliverable",
             "deliverable_id": "deliverable-b", "runtime": "codex", "status": "active"},
        ]

    def get_mission_status(self, *, deliverable_id, **_kwargs):
        task_id = "TASK-A" if deliverable_id == "deliverable-a" else "TASK-B"
        detail = {
            "task_id": task_id, "status": "Not Started",
            "dependency_state": {"ready": True, "satisfied": True},
            "active_claims": [], "provenance": {}, "workstream": "CO",
        }
        return {
            "deliverable_id": deliverable_id,
            "deliverable": {"id": deliverable_id, "status": "approved"},
            "linked_tasks": [{"task_id": task_id, "project_id": "switchboard",
                              "task_detail": detail}],
            "dispatch_scope": {"links": [{"task_id": task_id,
                                            "project_id": "switchboard",
                                            "automatic_dispatch_eligible": True}]},
            "next_actions": [{"action": "claim_task", "task_id": task_id,
                              "project_id": "switchboard", "lane": "CO"}],
        }

    def update_autopilot_scope(self, scope_id, **kwargs):
        self.calls.append(("scope_update", scope_id, kwargs))
        return {"scope_id": scope_id, **kwargs}

    def run_mission_coordinator_tick(self, *, idem_key, deliverable_id, policy, **kwargs):
        self.calls.append(("mission", deliverable_id, policy))
        if idem_key not in self.effects:
            self.cursor += 1
            self.effects[idem_key] = {
                "status": "wake_requested", "decision_id": f"decision-{self.cursor}",
                "dispatch": {"wake_id": f"wake-{self.cursor}"},
            }
        return dict(self.effects[idem_key])

    def list_wake_intents(self, *, task_id="", deliverable_id="", **_kwargs):
        return [
            dict(row) for row in self.wakes
            if (not task_id or row.get("task_id") == task_id)
            and (not deliverable_id
                 or (row.get("selector") or {}).get("deliverable_id")
                 == deliverable_id)
        ]


clock = Clock()
store = FakeStore(clock)
config = daemon_mod.DaemonConfig(
    profile_id="test", projects=("switchboard",), allowed_lanes=("CO", "COORD"),
    act=True, max_deliverables_per_tick=1, heartbeat_seconds=10,
    lease_ttl_seconds=30,
)

first = daemon_mod.CoordinatorDaemon(
    config, store_mod=store, instance_id="instance-one", clock=clock)
run1 = first.tick_project("switchboard")
ok(run1["status"] == "running" and run1["receipts"][0]["deliverable_id"] == "deliverable-a",
   "leader processes one bounded operator-started scope")
state1 = run1["state"]
ok(state1["sequence"] == 1 and state1["last_deliverable_id"] == "deliverable-a",
   "sequence and deliverable cursor persist after the idempotent effect")
policy = [call[2] for call in store.calls if call[0] == "mission"][-1]
ok(policy["allowed_lanes"] == ["CO", "COORD"] and policy["auto_wake"] is True,
   "project/lane policy and acting wake mode reach the mission coordinator")

second = daemon_mod.CoordinatorDaemon(
    config, store_mod=store, instance_id="instance-two", clock=clock)
standby = second.tick_project("switchboard")
ok(standby["status"] == "standby" and standby["lease"].get("conflict"),
   "a second live instance cannot control the same project")

clock.value += 31
run2 = second.tick_project("switchboard")
ok(run2["status"] == "running" and run2["receipts"][0]["deliverable_id"] == "deliverable-b",
   "replacement leader resumes after the durable deliverable cursor")

# Simulate a crash after an effect but before its state checkpoint by restoring
# the prior sequence/cursor. The wrapper receives the same idem key, so no second
# wake/effect is created.
effects_before = len(store.effects)
store.set_meta(daemon_mod._state_key("test"), state1, project="switchboard")
clock.value += 31
third = daemon_mod.CoordinatorDaemon(
    config, store_mod=store, instance_id="instance-three", clock=clock)
replay = third.tick_project("switchboard")
ok(replay["receipts"][0]["task_receipts"][0]["idem_key"] in store.effects
   and len(store.effects) == effects_before,
   "crash replay reuses the durable idempotency key without duplicating effects")

# A host can fail before it creates a claim, leaving the task snapshot unchanged.
# The terminal wake advances the daemon generation so the next tick is a real
# retry, while subsequent polls of that same generation remain idempotent.
store.wakes.append({
    "wake_id": "wake-terminal", "task_id": "TASK-B", "status": "failed",
    "selector": {"deliverable_id": "deliverable-b"},
})
retry_effects_before = len(store.effects)
store.set_meta(daemon_mod._state_key("test"), state1, project="switchboard")
clock.value += 31
retry_run = daemon_mod.CoordinatorDaemon(
    config, store_mod=store, instance_id="instance-retry", clock=clock,
).tick_project("switchboard")
retry_key = retry_run["receipts"][0]["task_receipts"][0]["idem_key"]
ok("wake-generation-1" in retry_key
   and len(store.effects) == retry_effects_before + 1,
   "terminal wake advances the durable retry generation without a task-state change")

policy_variant = daemon_mod.DaemonConfig(
    profile_id="test", projects=("switchboard",), allowed_lanes=("CO", "COORD"),
    act=True, max_deliverables_per_tick=1, heartbeat_seconds=10,
    lease_ttl_seconds=30, elastic_runtime_config_ref="ssm:/new-runtime-policy",
)
store.set_meta(daemon_mod._state_key("test"), state1, project="switchboard")
clock.value += 31
policy_run = daemon_mod.CoordinatorDaemon(
    policy_variant, store_mod=store, instance_id="instance-policy", clock=clock,
).tick_project("switchboard")
policy_key = policy_run["receipts"][0]["task_receipts"][0]["idem_key"]
ok(policy_key != retry_key and ":policy-" in policy_key,
   "a deployed dispatch-policy change cannot conflict with the prior receipt")

paused = daemon_mod.set_control(
    store, "switchboard", "test", actor="operator", paused=True, now=clock())
clock.value += 31
fourth = daemon_mod.CoordinatorDaemon(
    config, store_mod=store, instance_id="instance-four", clock=clock)
paused_run = fourth.tick_project("switchboard")
ok(paused["paused"] is True and paused_run["status"] == "paused",
   "operator can durably pause a project")
daemon_mod.set_control(
    store, "switchboard", "test", actor="operator", paused=False,
    pause_lane="CO", now=clock())
clock.value += 31
lane_run = daemon_mod.CoordinatorDaemon(
    config, store_mod=store, instance_id="instance-five", clock=clock).tick_project("switchboard")
lane_policy = [call[2] for call in store.calls if call[0] == "mission"][-1]
ok(lane_run["status"] == "running" and lane_policy["denied_lanes"] == ["CO"],
   "paused lanes are re-read and removed from automatic selection")

service = Path("deploy/projectplanner-coordinator-autopilot.service").read_text()
ok("Restart=always" in service and "coordinator_daemon.py run" in service
   and "PM_COORDINATOR_AUTOPILOT_ACT=1" in service,
   "systemd profile is persistent and active scopes are the arming boundary")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
