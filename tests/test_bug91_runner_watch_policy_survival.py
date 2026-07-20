#!/usr/bin/env python3
"""BUG-91: runner_watch survives the authoritative policy, and only when proven.

Observed live on host/steve-mbp-co16 (0.2.25): registration advertised
runner_watch, then the first heartbeat returned an authoritative execution
policy and apply_authoritative_execution_policy replaced the capability list
wholesale — the advertisement vanished one tick after it appeared.

The rule: runner_watch is a host-proven fact. The policy may select every other
capability, but it can neither strip a proven advertisement nor grant one the
host cannot back with a real PTY/relay path.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
from adapters import agent_host

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def inventory():
    return {
        "runtimes": [{"runtime": "codex", "lanes": [],
                      "capabilities": ["docs", "runner_watch"],
                      "policy": {}, "control": {}}],
        "limits": {"max_sessions": 2},
        "capacity": {},
    }


def response(capabilities):
    return {"authoritative_execution_policy": {
        "runtime": "codex", "allow_global_claim": False,
        "max_sessions": 2, "lane_mode": "all_project_lanes",
        "allow_work": True, "capabilities": capabilities,
        "revision": 7,
    }}


saved = agent_host.host_serves_runner_watch
try:
    # A proven host keeps the advertisement even though the policy's own
    # capability list does not mention it.
    agent_host.host_serves_runner_watch = lambda: True
    inv = inventory()
    changed = agent_host.apply_authoritative_execution_policy(
        inv, response(["docs", "python", "github", "tests"]))
    caps = inv["runtimes"][0]["capabilities"]
    ok(changed is True, "the authoritative policy still applies")
    ok("runner_watch" in caps,
       "a proven host keeps runner_watch after the policy replaces the capability list")
    ok(caps.count("runner_watch") == 1,
       "repeated policy applications never duplicate the advertisement")
    agent_host.apply_authoritative_execution_policy(
        inv, response(["docs", "python", "github", "tests"]))
    ok(inv["runtimes"][0]["capabilities"].count("runner_watch") == 1,
       "a second heartbeat tick keeps exactly one advertisement")

    # An unproven host cannot be granted the capability by policy — a false
    # positive routes Watch-requiring work to a host that cannot show the run.
    agent_host.host_serves_runner_watch = lambda: False
    inv = inventory()
    agent_host.apply_authoritative_execution_policy(
        inv, response(["docs", "runner_watch"]))
    ok("runner_watch" not in inv["runtimes"][0]["capabilities"],
       "policy cannot grant runner_watch to a host that cannot actually serve Watch")
    ok("docs" in inv["runtimes"][0]["capabilities"],
       "every other policy-selected capability is applied unchanged")

    # An invalid policy is still refused before any capability logic runs.
    agent_host.host_serves_runner_watch = lambda: True
    inv = inventory()
    refused = agent_host.apply_authoritative_execution_policy(
        inv, {"authoritative_execution_policy": {
            "runtime": "codex", "allow_global_claim": True, "max_sessions": 2,
            "lane_mode": "all_project_lanes", "capabilities": []}})
    ok(refused is False and inv["runtimes"][0]["capabilities"] == ["docs", "runner_watch"],
       "an invalid policy is refused without touching the advertised capabilities")
finally:
    agent_host.host_serves_runner_watch = saved

print(f"\nBUG-91 runner_watch policy survival: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
