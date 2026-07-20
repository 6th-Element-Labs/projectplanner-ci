#!/usr/bin/env python3
"""UI-28 acceptance: one-click exact-task fanout across Mac and AWS capacity."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

tmp = Path(tempfile.mkdtemp(prefix="ui28-"))
os.environ.update({
    "PM_SWITCHBOARD_DB_PATH": str(tmp / "switchboard.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(tmp / "registry.db"),
    "PM_DYNAMIC_PROJECTS_DIR": str(tmp / "projects"),
    "PM_PROJECT": "switchboard",
})
(tmp / "projects").mkdir()

import store  # noqa: E402
import coordinator_daemon  # noqa: E402
import mission_coordinator  # noqa: E402
from adapters import agent_host  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


store.init_db("switchboard")
created = store.begin_agent_host_enrollment(
    owner_user_id="owner-ui28", requested_host_id="host/ui28-mac",
    project_allowlist=["switchboard"], project="switchboard")
policy = (created.get("enrollment") or {}).get("execution_policy") or {}
ok(policy.get("lane_mode") == "all_project_lanes"
   and policy.get("lanes") == [] and policy.get("max_sessions") == 8
   and policy.get("personal_wakes_only") is False,
   "personal Macs enroll as project-wide eight-session Autopilot hosts")

inventory = {
    "host_id": "host/ui28-mac",
    "repo_root": "/tmp/repo",
    "policy": {"allow_work": True, "allow_global_claim": False},
    "runtimes": [{
        "runtime": "codex", "lanes": ["ADAPTER"],
        "capabilities": ["docs"],
        "policy": {"allow_work": True, "allow_global_claim": False},
        "control": {},
    }],
    "limits": {"max_sessions": 1},
    "capacity": {"placement": {"concurrency": {"max_sessions": 1}}},
}
agent_host.active_session_count = lambda _inventory: 2
changed = agent_host.apply_authoritative_execution_policy(
    inventory, {"authoritative_execution_policy": policy})
ok(changed and inventory["limits"]["max_sessions"] == 8
   and inventory["runtimes"][0]["lanes"] == []
   and inventory["capacity"]["headroom"] == 6,
   "host hot-applies server concurrency without reinstall or re-enrollment")
cross_lane_wake = {
    "task_id": "ARCH-MS-119",
    "selector": {"runtime": "codex", "lane": "ARCH-MS"},
    "policy": {"mode": "claim_next"},
}
ok(agent_host.eligible_runtime(cross_lane_wake, inventory) is not None,
   "project-wide Mac accepts an exact task from any project lane")


class LifecycleStore:
    def __init__(self):
        self.starts = []

    def record_coordinator_decision(self, **kwargs):
        return {"decision_id": "decision-ui28", **kwargs}

    def append_activity(self, *_args, **_kwargs):
        return 1


lifecycle_store = LifecycleStore()
mission_status = {
    "deliverable_id": "deliverable-ui28",
    "progress": {"linked_task_count": 1, "done_with_proof_ratio": 0},
    "dispatch_scope": {"blocking_task_count": 1, "blocking_done_with_proof_ratio": 0},
    "next_actions": [{
        "action": "claim_task", "task_id": "ARCH-MS-119",
        "project_id": "switchboard", "lane": "ARCH-MS", "title": "Fan out",
    }],
}
def task_starter(task_id, **kwargs):
    lifecycle_store.starts.append({"task_id": task_id, **kwargs})
    return {"action": "started", "started": True, "wake_id": "wake-ui28",
            "role": kwargs.get("role"), "placement": "mac_preferred"}


tick = mission_coordinator.run_coordinator_tick(
    mission_status, mission_project="switchboard", store_mod=lifecycle_store,
    policy={"auto_refresh_brief": False, "auto_start": True},
    task_starter=task_starter, actor="ui28-test", idem_key="ui28-test")
started = lifecycle_store.starts[0] if lifecycle_store.starts else {}
ok(tick.get("status") == "session_ensured"
   and started.get("task_id") == "ARCH-MS-119"
   and started.get("project") == "switchboard"
   and started.get("role") == "implementation",
   "Autopilot ensures the exact task through start_task")
source = Path("dispatch.py").read_text()
ok("DOGFOOD-20" in source and "mac_preferred" in source
   and "aws_overflow" in source and "PM_AUTOPILOT_COFLEET" not in source,
   "start_task prefers Mac and gates AWS overflow on DOGFOOD-20")

config = coordinator_daemon.DaemonConfig.from_env({
    "PM_COORDINATOR_AUTOPILOT_PROJECTS": "switchboard",
    "PM_COORDINATOR_AUTOPILOT_RUNTIME_CONFIG_REF":
        "ssm:/switchboard/co/runtime/autopilot",
    "PM_COORDINATOR_AUTOPILOT_ACT": "1",
})
ok(config.max_deliverables_per_tick == 64
   and config.max_tasks_per_scope_tick == 64
   and config.act is True,
   "one sweep schedules sixty selected deliverables without an eight-task throttle")

app_source = Path("static/app.js").read_text()
runner_source = Path("static/js/runner-session.js").read_text()
mission_source = Path("static/js/mission.js").read_text()
ok("data-runner-watch-task" in app_source and "data-runner-watch-task" in runner_source,
   "Fleet exposes Watch/Chat entry points for every running task")
ok("Waiting for execution capacity" in mission_source and "Open Fleet" in mission_source,
   "deliverable UI explains capacity waits and links directly to Fleet")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
