"""BUG-161: authoritative policy cannot strip host-proven lease capability."""
from __future__ import annotations

import os

from path_setup import ROOT  # noqa: F401

from adapters import agent_host


saved = {
    key: os.environ.get(key)
    for key in ("PM_RUNNER_LEASE_ENFORCEMENT", "PM_AGENT_HOST_SERVES_RUNNER_WATCH")
}
try:
    os.environ["PM_RUNNER_LEASE_ENFORCEMENT"] = "1"
    os.environ["PM_AGENT_HOST_SERVES_RUNNER_WATCH"] = "0"
    inventory = agent_host.default_inventory()
    agent_host.apply_authoritative_execution_policy(inventory, {
        "authoritative_execution_policy": {
            "runtime": "codex",
            "allow_global_claim": False,
            "allow_work": True,
            "lane_mode": "all_project_lanes",
            "lanes": [],
            "max_sessions": 4,
            "capabilities": ["docs", "github", "python", "tests"],
            "revision": 5,
        },
    })
    capabilities = inventory["runtimes"][0]["capabilities"]
    assert "execution_lease_v2" in capabilities
    assert "runner_lease_enforcement" in capabilities
finally:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

print("BUG-161 lease capability policy survival: PASS")
