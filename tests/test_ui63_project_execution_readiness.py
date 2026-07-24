#!/usr/bin/env python3
"""UI-63: authoritative project execution readiness contract and surfaces."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401
from switchboard.storage.repositories import project_execution_readiness as readiness


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


class ProviderRepository:
    def __init__(self, connections):
        self.connections = connections

    def list_metadata(self, **_kwargs):
        return list(self.connections)


class SCMRepository:
    def __init__(self, connection):
        self.connection = connection

    def get(self, connection_id):
        if not self.connection or connection_id != self.connection["connection_id"]:
            raise readiness.SCMConnectionError("missing", "missing")
        return dict(self.connection)


def policy():
    return {
        "readiness": {"passed": True},
        "runtimes": {"allowed": ["codex"], "default": "codex"},
        "placement": {
            "host_classes": ["persistent", "ephemeral"],
            "trust_zones": ["trusted"],
            "burst": {"enabled": True, "max_concurrent_ephemeral": 2},
        },
        "providers": {"selectors": [{
            "provider": "openai-codex",
            "connection_reference": "provider/atlas",
        }]},
        "scm": {"provider": "github", "connection_reference": "scm/atlas"},
        "autopilot": {"enabled": True, "profile_id": "safe"},
        "lifecycle": {"revision": 4, "status": "active"},
    }


def topology():
    return {
        "valid": True,
        "roles": {"canonical": {"repo": "example/atlas", "default_branch": "main"}},
    }


def host():
    return {
        "host_id": "host/atlas",
        "available_sessions": 1,
        "runtimes": [{"runtime": "codex", "local_auth": {"available": True}}],
        "capacity": {"placement": {
            "host_class": "persistent",
            "wakeable": True,
            "drain_state": "accepting",
            "projects": ["atlas"],
            "repositories": ["example/atlas"],
            "trust_zones": ["trusted"],
        }},
    }


def evaluate(configured):
    missing_policy = {
        "readiness": {
            "passed": False,
            "reason_code": "project_execution_policy_missing",
            "message": "policy missing",
            "missing": ["runtimes.allowed"],
        },
        "runtimes": {"allowed": []},
        "placement": {"host_classes": [], "burst": {}},
        "providers": {"selectors": []},
        "scm": {},
        "autopilot": {"enabled": False},
        "lifecycle": {"revision": 0},
    }
    provider = ProviderRepository([{
        "credential_reference": "provider/atlas",
        "provider": "openai-codex",
        "lifecycle_state": "active",
        "refresh_state": "ready",
    }] if configured else [])
    scm = SCMRepository({
        "connection_id": "scm/atlas",
        "lifecycle_state": "active",
        "project_allowlist": ["atlas"],
        "repository_allowlist": ["example/atlas"],
        "operation_scopes": ["clone", "fetch", "push", "create_pr", "merge"],
    } if configured else None)
    with (
        patch.object(readiness, "has_project", lambda project: project == "atlas"),
        patch.object(
            readiness, "get_project_repo_topology",
            lambda _project: topology() if configured
            else {"valid": False, "missing": ["canonical"]}),
        patch.object(
            readiness, "get_project_execution_policy",
            lambda _project: policy() if configured else missing_policy),
        patch.object(readiness, "default_provider_credential_repository", provider),
        patch.object(readiness, "default_scm_connection_repository", scm),
        patch.object(
            readiness, "list_agent_hosts",
            lambda **_kwargs: [host()] if configured else []),
    ):
        return readiness.get_project_execution_readiness("atlas")


red = evaluate(False)
ok(red["schema"] == readiness.SCHEMA and red["passed"] is False
   and red["status"] == "blocked",
   "unconfigured Atlas is blocked by the versioned authoritative gate")
ok(bool(red["blockers"]) and all(
    blocker["code"] and blocker["category"] and blocker["message"]
    and blocker["repair"] and blocker["blocking"] is True
    for blocker in red["blockers"]),
   "every red signal is a typed blocker with operator repair guidance")

green = evaluate(True)
ok(green["passed"] is True and green["status"] == "ready"
   and green["reason_code"] == "",
   "configured Atlas transitions to green")
ok(green["states"]["configuration"]["passed"] is True
   and green["states"]["persistent"]["eligible_host_ids"] == ["host/atlas"]
   and green["states"]["ephemeral"]["burst_enabled"] is True
   and green["states"]["autopilot"]["status"] == "ready",
   "green readiness exposes configuration, persistent, ephemeral, and Autopilot states")

projects_api = (ROOT / "src/switchboard/api/routers/projects.py").read_text()
projects_mcp = (ROOT / "src/switchboard/mcp/tools/projects.py").read_text()
start = (ROOT / "src/switchboard/application/commands/task_execution.py").read_text()
settings = (ROOT / "static/js/settings.js").read_text()
runner = (ROOT / "static/js/runner-session.js").read_text()
ok('"/api/projects/{project}/execution_readiness"' in projects_api
   and "def get_project_execution_readiness(" in projects_mcp
   and '"get_project_execution_readiness"' in projects_mcp,
   "REST and MCP expose the same readiness function")
ok("get_project_execution_readiness(project)" in start
   and "execution_readiness=readiness" in start,
   "Start reruns and returns the authoritative gate")
ok("settings-execution-readiness" in settings
   and "data-readiness-state" in settings
   and "Open execution readiness" in runner,
   "project Settings and task Start UI surface states and repair guidance")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
