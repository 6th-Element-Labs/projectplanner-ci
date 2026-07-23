#!/usr/bin/env python3
"""SIMPLIFY-20: prevent new autonomous managed-process killers."""
from pathlib import Path

from path_setup import ROOT


agent_host = (ROOT / "adapters" / "agent_host.py").read_text(encoding="utf-8")
co_drain = (ROOT / "adapters" / "co_drain.py").read_text(encoding="utf-8")
dispatch = (
    ROOT / "src" / "switchboard" / "application" / "commands"
    / "connect_dispatch.py"
).read_text(encoding="utf-8")

# CO drain is automatic capacity-plane behavior. It may make the execution
# lease due, but may not signal the process itself.
assert 'supervisor(\n                "kill"' not in co_drain
assert '"lease_stop", runner_id' in co_drain
assert "P_RUNNER_LEASE_DUE" in agent_host
assert "outcomes = expire_runner_leases(inventory)" in agent_host

# One rollback flag controls both placement and enforcement. A second lifecycle
# clock would let capable and enforcing fleets disagree.
assert "PM_EXECUTION_LIFECYCLE_V1" not in dispatch
assert 'PM_RUNNER_LEASE_ENFORCEMENT", "1"' in dispatch
assert 'PM_RUNNER_LEASE_ENFORCEMENT", "1"' in agent_host

# These are the only production functions allowed to issue a managed-process
# kill: lease expiry, explicit operator control, and fail-closed spawn cleanup.
allowed = {
    "expire_runner_leases",
    "handle_runner_controls",
    "_finalize_bound_runner",
    "_submit_bound_finalizer",
    "handle_pending_wakes",
    "run_once",
}
current = ""
offenders = []
for number, line in enumerate(agent_host.splitlines(), 1):
    if line.startswith("def "):
        current = line.split("def ", 1)[1].split("(", 1)[0]
    if 'supervisor_action("kill"' in line and current not in allowed:
        offenders.append((number, current, line.strip()))
assert not offenders, offenders

print("SIMPLIFY-20 kill census passed")
