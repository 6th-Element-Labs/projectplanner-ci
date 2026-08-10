#!/usr/bin/env python3
"""COORD-48: registry discovery without policy-based launch refusal."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
import coordinator_daemon


class Store:
    def __init__(self):
        self.ids = ["atlas", "switchboard"]
        self.policies = {
            project: {
                "configured": True,
                "valid": True,
                "lifecycle": {"status": "active"},
                "autopilot": {"enabled": True},
            }
            for project in self.ids
        }
        self.readiness = {
            project: {"passed": True, "status": "ready", "blockers": []}
            for project in self.ids
        }
        self.activities = []
        self.leases = []

    def project_ids(self):
        return list(self.ids)

    def get_project_execution_policy(self, project):
        return dict(self.policies[project])

    def get_project_execution_readiness(self, project):
        return dict(self.readiness[project])

    def append_activity(self, kind, actor, payload, **kwargs):
        self.activities.append((kind, actor, payload, kwargs))

    def heartbeat(self, *_args, **_kwargs):
        return {"ok": True}

    def get_meta(self, *_args, **_kwargs):
        return {}

    def set_meta(self, *_args, **_kwargs):
        return None

    def claim_resources(self, *_args, **kwargs):
        self.leases.append(kwargs.get("project"))
        return {"lease_id": f"lease-{kwargs.get('project')}", "expires_at": 999}

    def list_autopilot_scopes(self, **_kwargs):
        return []

    def _activity_cursor(self, _project):
        return 0


store = Store()
daemon = coordinator_daemon.CoordinatorDaemon(
    coordinator_daemon.DaemonConfig(projects=()), store_mod=store,
    instance_id="coord48", clock=lambda: 1)

first = daemon.tick()
assert [row["project"] for row in first["projects"]] == ["atlas", "switchboard"]
assert store.leases == ["atlas", "switchboard"]

# Add/remove is observed on the next sweep without constructing a new daemon.
store.ids.append("new-project")
store.policies["new-project"] = {
    "configured": True, "valid": True, "lifecycle": {"status": "active"},
    "autopilot": {"enabled": True},
}
store.readiness["new-project"] = {
    "passed": False, "status": "blocked", "reason_code": "scm_missing",
    "blockers": [{"code": "scm_missing", "category": "scm", "blocking": True}],
}
store.policies["atlas"]["autopilot"]["enabled"] = False
second = daemon.tick()
assert [row["project"] for row in second["projects"]] == [
    "new-project", "switchboard",
]
advisory_red = second["projects"][0]
assert advisory_red["status"] == "idle"
assert store.leases == ["atlas", "switchboard", "new-project", "switchboard"]

# The legacy setting is an emergency ceiling, not a source of authority.
ceiling = coordinator_daemon.CoordinatorDaemon(
    coordinator_daemon.DaemonConfig(projects=("switchboard",)),
    store_mod=store, instance_id="ceiling", clock=lambda: 1)
assert ceiling.discover_projects() == ("switchboard",)

print("COORD-48 dynamic project discovery: 3 passed")
