#!/usr/bin/env python3
"""DHCP admit: armed scopes boot; readiness never gates projects.

COORD-48 discovery + a blanket readiness refusal starved unconfigured dogfood
boards that already had Autopilot scopes. Align with BUG-190 / ADR-0008:
scope lease is boot authority; readiness refuses only configured policies.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
import coordinator_daemon


class Store:
    def __init__(self):
        self.ids = ["switchboard", "atlas"]
        self.policies = {
            "switchboard": {
                "configured": False, "valid": False,
                "lifecycle": {"status": "draft"},
                "autopilot": {"enabled": False},
            },
            "atlas": {
                "configured": True, "valid": True,
                "lifecycle": {"status": "active"},
                "autopilot": {"enabled": True},
            },
        }
        self.readiness = {
            "switchboard": {
                "passed": False, "status": "blocked",
                "reason_code": "project_execution_policy_missing",
                "blockers": [{"code": "project_execution_policy_missing"}],
            },
            "atlas": {"passed": True, "status": "ready", "blockers": []},
        }
        self.scopes = {
            "switchboard": [{"scope_id": "scope-sb", "status": "active"}],
            "atlas": [],
        }
        self.activities = []

    def project_ids(self):
        return list(self.ids)

    def get_project_execution_policy(self, project):
        return dict(self.policies[project])

    def get_project_execution_readiness(self, project):
        return dict(self.readiness[project])

    def list_autopilot_scopes(self, **kwargs):
        return list(self.scopes.get(kwargs.get("project"), []))

    def append_activity(self, kind, actor, payload, **kwargs):
        self.activities.append((kind, actor, payload, kwargs))


store = Store()
daemon = coordinator_daemon.CoordinatorDaemon(
    coordinator_daemon.DaemonConfig(projects=()), store_mod=store,
    instance_id="dhcp-admit", clock=lambda: 1)

# Unconfigured switchboard with an armed scope is admitted (DHCP).
found = daemon.discover_projects()
assert "switchboard" in found, found
assert "atlas" in found, found

# Drop the scope → unconfigured board leaves discovery.
store.scopes["switchboard"] = []
assert "switchboard" not in daemon.discover_projects()

# Restore scope; even an opted-in Atlas treats red readiness as advisory.
store.scopes["switchboard"] = [{"scope_id": "scope-sb", "status": "active"}]
store.readiness["atlas"] = {
    "passed": False, "status": "blocked", "reason_code": "scm_missing",
    "blockers": [{"code": "scm_missing"}],
}
daemon._admitted_projects = daemon.discover_projects()

# Neither unconfigured nor configured red readiness may early-return. Exercise
# only the prelude by temporarily short-circuiting after admission.
orig_register = daemon._register_or_heartbeat
seen = []

def _mark(project):
    seen.append(project)
    raise RuntimeError("gate-passed")

daemon._register_or_heartbeat = _mark  # type: ignore[method-assign]
try:
    daemon.tick_project("switchboard")
    raise AssertionError("expected gate-passed short-circuit")
except RuntimeError as exc:
    assert str(exc) == "gate-passed"
assert seen == ["switchboard"], seen

try:
    daemon.tick_project("atlas")
    raise AssertionError("expected gate-passed short-circuit")
except RuntimeError as exc:
    assert str(exc) == "gate-passed"
assert seen == ["switchboard", "atlas"], seen
daemon._register_or_heartbeat = orig_register  # type: ignore[method-assign]

print("COORD-48/BUG-190 DHCP scope admit: passed")
