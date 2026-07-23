"""BUG-161: enrollment accepts only the host-proven lease capability pair."""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401

from switchboard.storage.repositories import coordination


EXECUTION = {
    "runtime": "codex",
    "allow_work": True,
    "allow_global_claim": False,
    "lanes": [],
    "capabilities": ["docs", "github", "python", "tests"],
    "max_sessions": 16,
    "local_auth_required": False,
}
IDENTITY = {
    "required": True,
    "owner_user_id": "user-1",
    "tenant_allowlist": ["org-1"],
    "project_allowlist": ["switchboard"],
    "provider_allowlist": ["openai-codex"],
    "execution_policy": EXECUTION,
}
CAPACITY = {
    "owner": {
        "user_id": "user-1",
        "tenant_allowlist": ["org-1"],
        "project_allowlist": ["switchboard"],
        "provider_allowlist": ["openai-codex"],
    },
}
POLICY = {
    "allow_work": True,
    "allow_global_claim": False,
}


def inventory(capabilities):
    return [{
        "runtime": "codex",
        "lanes": [],
        "capabilities": capabilities,
        "policy": POLICY,
    }]


base = ["docs", "github", "python", "tests"]
lease_capabilities = ["execution_lease_v2", "runner_lease_enforcement"]

assert coordination._enrollment_inventory_error(
    IDENTITY,
    CAPACITY,
    "switchboard",
    runtimes=inventory(base + lease_capabilities + ["runner_watch"]),
    limits={"max_sessions": 16},
) is None

refused = coordination._enrollment_inventory_error(
    IDENTITY,
    CAPACITY,
    "switchboard",
    runtimes=inventory(base + lease_capabilities + ["operator_grant_bypass"]),
    limits={"max_sessions": 16},
)
assert refused and refused["error_code"] == "host_enrollment_policy_mismatch"

print("BUG-161 enrollment gate lease capabilities: PASS")
