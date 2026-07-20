#!/usr/bin/env python3
"""BUG-91: never place Watch-requiring work on a host that cannot serve Watch.

Measured on the live board: all 84 CO-fleet AWS runner rows had pty=false,
stream_bind=false, runner_open=false and runner_inject=false, while 58 rows on
the persistent Mac had the full quad. Those AWS hosts still accepted the work,
so clicking the task could only ever produce a refusal.

The host inventories that produced that split (from list_agent_hosts):

  host/steve-mbp-co16      agent_host_version 0.2.24  policy.allow_work true
  host/i-0c0f00f13dac0714d agent_host_version 0.1.0   policy.allow_work false

Rather than sniff version strings, a host self-declares `runner_watch` when its
build runs the supervisor PTY bridge and outbound relay. Older builds simply
never advertise it and are skipped.
"""
from __future__ import annotations

from path_setup import ROOT  # noqa: F401
import coordinator_dispatch as dispatch

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


NOW = 1_784_500_000.0


def host(host_id, capabilities):
    return {
        "host_id": host_id,
        "stale": False,
        "heartbeat_at": NOW - 5,
        "heartbeat_ttl_s": 60,
        "runtimes": [{"runtime": "codex", "lanes": ["SEG"],
                      "capabilities": list(capabilities)}],
    }


# The real shapes: the Mac advertises the new capability, the AWS workers carry
# the CO-fleet profile with no watch capability at all.
mac = host("host/steve-mbp-co16", ["docs", "github", "python", "tests", "runner_watch"])
aws_a = host("host/i-0c0f00f13dac0714d",
             ["docs", "python", "github", "tests", "co_fleet", "claude_code", "codex_cli"])
aws_b = host("host/i-071611fa6b75a6e0c",
             ["docs", "python", "github", "tests", "co_fleet", "claude_code", "codex_cli"])
snapshot = {"observed_at": NOW, "hosts": [aws_a, aws_b, mac], "agents": [], "tasks": []}

ok(dispatch.host_serves_runner_watch(mac) is True,
   "a host advertising runner_watch is recognised as Watch-capable")
ok(dispatch.host_serves_runner_watch(aws_a) is False,
   "a CO-fleet worker that advertises no watch capability is not Watch-capable")

everything = dispatch._active_hosts(snapshot, lane="SEG", runtime="codex")
ok({h["host_id"] for h in everything}
   == {"host/steve-mbp-co16", "host/i-0c0f00f13dac0714d", "host/i-071611fa6b75a6e0c"},
   "without the requirement every live SEG host is eligible, as before")

watchable = dispatch._active_hosts(snapshot, lane="SEG", runtime="codex",
                                   require_runner_watch=True)
ok([h["host_id"] for h in watchable] == ["host/steve-mbp-co16"],
   "Watch-requiring work is placed only on the host that can actually show it")

# The escalation must name the real reason, not an indistinguishable
# "no eligible host" that hides a fleet running an obsolete Agent Host build.
aws_only = {"observed_at": NOW, "hosts": [aws_a, aws_b], "agents": [], "tasks": []}
ok(dispatch._active_hosts(aws_only, lane="SEG", runtime="codex",
                          require_runner_watch=True) == [],
   "an all-AWS fleet yields no Watch-capable host rather than a silent bad placement")

# Ships OFF: every host registered before the advertising build carries no
# `runner_watch` capability, so enabling this on deploy would starve dispatch
# fleet-wide. Rollout order is agent hosts first, then flip the flag.
ok(dispatch._normalize_policy({})["require_runner_watch"] is False,
   "the requirement ships off so deploying it cannot halt the whole fleet")
ok(dispatch._normalize_policy({"require_runner_watch": True})["require_runner_watch"] is True,
   "an operator enables the requirement once hosts advertise the capability")

# A host advertising several runtimes qualifies when any of them serves Watch.
mixed = {"host_id": "host/mixed", "stale": False, "heartbeat_at": NOW - 5,
         "heartbeat_ttl_s": 60,
         "runtimes": [{"runtime": "claude-code", "capabilities": ["docs"]},
                      {"runtime": "codex", "capabilities": ["runner_watch"]}]}
ok(dispatch.host_serves_runner_watch(mixed) is True,
   "a multi-runtime host qualifies when any runtime serves Watch")

# Malformed advertisements must fail closed, never crash the dispatch tick.
for broken, label in ((None, "None runtimes"), ("nonsense", "string runtimes"),
                      ([None, 42], "junk runtime entries")):
    ok(dispatch.host_serves_runner_watch({"runtimes": broken}) is False,
       f"a host with {label} fails closed instead of raising")

print(f"\nBUG-91 watch-capable placement: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
