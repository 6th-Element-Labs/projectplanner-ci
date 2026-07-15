#!/usr/bin/env python3
"""Merge-queue liveness monitor — evaluate_groups verdict logic (pure, no network)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import merge_queue_health as m  # noqa: E402


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS ", message)


NOW = 1_000_000.0


def grp(ref, state, age_min):
    return {"ref": f"refs/heads/gh-readonly-queue/master/{ref}", "sha": "sha-" + ref,
            "formed_epoch": NOW - age_min * 60, "gate_state": state}


# Healthy cases -----------------------------------------------------------------------
ok(m.evaluate_groups([], now_epoch=NOW)["stuck"] is False, "no merge groups -> healthy (idle queue)")
ok(m.evaluate_groups([grp("pr-1", "pending", 3)], now_epoch=NOW, stuck_min=15)["stuck"] is False,
   "a young pending group (mid-CI) is not stuck")
ok(m.evaluate_groups([grp("pr-2", "failure", 40)], now_epoch=NOW, stuck_min=15)["stuck"] is False,
   "an old but terminal (failure) group is a red suite, not a liveness alert")
ok(m.evaluate_groups([grp("pr-3", "success", 40)], now_epoch=NOW, stuck_min=15)["stuck"] is False,
   "an old success is healthy")

# Stuck cases -------------------------------------------------------------------------
v = m.evaluate_groups([grp("pr-4", "pending", 25)], now_epoch=NOW, stuck_min=15)
ok(v["stuck"] is True and any("pr-4" in r for r in v["reasons"]),
   "old pending group -> STUCK with a reason")
ok(m.evaluate_groups([grp("pr-5", "missing", 25)], now_epoch=NOW, stuck_min=15)["stuck"] is True,
   "old group with no gate status at all -> STUCK (box/wiring dead)")

# Mixed: only the stuck one is flagged ------------------------------------------------
v = m.evaluate_groups([grp("pr-6", "success", 40), grp("pr-7", "pending", 30)],
                      now_epoch=NOW, stuck_min=15)
ok(v["stuck"] is True and len(v["reasons"]) == 1 and "pr-7" in v["reasons"][0],
   "flags only the stuck group in a mixed set")

# Threshold boundary ------------------------------------------------------------------
ok(m.evaluate_groups([grp("pr-8", "pending", 14)], now_epoch=NOW, stuck_min=15)["stuck"] is False,
   "pending just under the threshold is not yet stuck")

# Timestamp parsing tolerates Z / offset / naive (a format quirk must not false-alert) -----
ok(abs(m._iso_epoch("2026-07-15T00:00:00Z") - m._iso_epoch("2026-07-15T00:00:00+00:00")) < 1e-6,
   "_iso_epoch: 'Z' and '+00:00' parse to the same instant")
ok(abs(m._iso_epoch("2026-07-15T00:00:00Z") - m._iso_epoch("2026-07-15T08:00:00+08:00")) < 1e-6,
   "_iso_epoch: an explicit offset resolves to the same instant as UTC")
try:
    m._iso_epoch("")
    ok(False, "_iso_epoch: empty timestamp should raise")
except ValueError:
    ok(True, "_iso_epoch: empty timestamp raises (caught by the monitor's backstop)")

print("\nAll merge_queue_health tests passed.")
raise SystemExit(0)
