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
                "status": "session_ensured", "decision_id": f"decision-{self.cursor}",
                "dispatch": {"wake_id": f"wake-{self.cursor}", "role": "implementation"},
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

# BUG-143: flow-role links are structurally eligible; context links remain excluded.
nonblocking_detail = {
    "task_id": "TASK-NONBLOCKING", "status": "Not Started",
    "dependency_state": {"ready": True, "satisfied": True},
    "active_claims": [], "provenance": {},
}
nonblocking_status = {
    "deliverable": {"id": "deliverable-explicit", "status": "approved"},
    "linked_tasks": [
        {"task_id": "TASK-NONBLOCKING", "project_id": "switchboard",
         "task_detail": nonblocking_detail},
        {"task_id": "TASK-PARKED", "project_id": "switchboard",
         "task_detail": {**nonblocking_detail, "task_id": "TASK-PARKED"}},
    ],
    "dispatch_scope": {"links": [
        {"task_id": "TASK-NONBLOCKING", "project_id": "switchboard",
         "automatic_dispatch_eligible": True,
         "reason": "automatic_flow"},
        {"task_id": "TASK-PARKED", "project_id": "switchboard",
         "automatic_dispatch_eligible": False, "reason": "context_role:parked"},
    ]},
    "next_actions": [],
}
explicit_scope = {"scope_type": "deliverable", "deliverable_id": "deliverable-explicit"}
explicit_candidates = first._scope_candidates(explicit_scope, nonblocking_status)
ok([row["task_id"] for row in explicit_candidates] == ["TASK-NONBLOCKING"],
   "Start deliverable opts ordinary non-blocking work into exact-task dispatch")
ok(first._scope_complete(explicit_scope, nonblocking_status) is False,
   "operator-started non-blocking work remains part of scope completion")
nonblocking_detail["status"] = "Done"
nonblocking_detail["provenance"] = {"terminal": True}
ok(first._scope_complete(explicit_scope, nonblocking_status) is True,
   "scope completes after explicitly covered non-blocking work reaches terminal provenance")

run1 = first.tick_project("switchboard")
ok(run1["status"] == "running" and run1["receipts"][0]["deliverable_id"] == "deliverable-a",
   "janitor observes one bounded operator-started scope")
work_actions = {
    key: value for key, value in run1["lifecycle"]["action_census"].items()
    if key != "cleanup"
}
ok(not run1["decision_stream"] and all(value == 0 for value in work_actions.values()),
   "global daemon action census contains no work-driving or messaging effects")
ok(not [call for call in store.calls if call[0] == "mission"],
   "global daemon never invokes the scoped mission coordinator")
state1 = run1["state"]
ok(state1["sequence"] == 1 and state1["last_deliverable_id"] == "deliverable-a",
   "janitor cursor persists after the bounded observation")

second = daemon_mod.CoordinatorDaemon(
    config, store_mod=store, instance_id="instance-two", clock=clock)
standby = second.tick_project("switchboard")
ok(standby["status"] == "standby" and standby["lease"].get("conflict"),
   "a second live instance cannot control the same project")

clock.value += 31
run2 = second.tick_project("switchboard")
ok(run2["status"] == "running" and run2["receipts"][0]["deliverable_id"] == "deliverable-b",
   "replacement janitor resumes after the durable scope cursor")

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
ok(lane_run["status"] == "running"
   and not [call for call in store.calls if call[0] == "mission"],
   "lane controls cannot turn the janitor into a work scheduler")

service = Path("deploy/projectplanner-coordinator-autopilot.service").read_text()
ok("Restart=always" in service and "coordinator_daemon.py run" in service
   and "PM_COORDINATOR_AUTOPILOT_ACT=1" in service,
   "systemd profile is persistent and ships the scoped completion driver (ACT=1)")
daemon_source = Path("coordinator_daemon.py").read_text()
ok("run_mission_coordinator_tick" not in daemon_source
   and "review_steward" not in daemon_source
   and "merge_steward" not in daemon_source
   and "send_agent_message(" not in daemon_source,
   "janitor source contains no independent work scheduler or messaging path")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
