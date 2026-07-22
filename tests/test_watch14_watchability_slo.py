#!/usr/bin/env python3
"""WATCH-14: scoped sampled watchability SLO and tighten-only target."""
from __future__ import annotations

import os
import json
from pathlib import Path

from path_setup import ROOT  # noqa: F401

import bridge_attachment_monitor as monitor  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {message}")
    else:
        failed += 1
        print(f"  FAIL  {message}")


rows = [
    {
        "runner_session_id": "run-host-a", "task_id": "WATCH-14",
        "host_id": "host-a", "status": "running",
        "metadata": {"native_host_execution": True, "pty": True},
        "control": {"runner_open": True},
    },
    {
        "runner_session_id": "run-cloud", "task_id": "CLOUD-1",
        "host_id": "cloud", "status": "running", "metadata": {},
    },
    {
        "runner_session_id": "run-exiting", "task_id": "OLD-1",
        "host_id": "host-b", "status": "stopping",
        "metadata": {"native_host_execution": True, "pty": True},
    },
]
attachments = {"run-host-a": False, "run-cloud": False, "run-exiting": False}


def sessions(_project):
    return list(rows)


def attached(sid):
    return attachments.get(sid)


def sample(now):
    return monitor.snapshot(
        "switchboard", sessions_provider=sessions, attachment_provider=attached,
        event_sink=lambda *_args, **_kwargs: None, now=now, window_s=300,
    )["watchability_slo"]


monitor.reset_for_tests()
baseline = json.loads((Path(ROOT) / "perf" / "watchability_slo.json").read_text())
ok(baseline["target"] >= 0.99,
   "the committed ratchet cannot relax below the product's 99% target")
os.environ["PM_RUNNER_WATCHABILITY_STARTUP_GRACE_S"] = "300"
os.environ["PM_RUNNER_WATCHABILITY_TARGET"] = "0.99"
sample(1000)
before_bound = sample(1299)
ok(before_bound["eligible_running_minutes"] == 0,
   "startup grace excludes a native PTY run before its bounded attach deadline")

after_bound = sample(1360)
ok(after_bound["eligible_running_minutes"] == 1.0
   and after_bound["attached_minutes"] == 0.0,
   "a run that never attaches becomes denominator and violation after the bound")
ok(set(after_bound["by_host"]) == {"host-a"},
   "cloud/no-PTY and terminal runs are excluded from the fleet and host denominator")

attachments["run-host-a"] = True
sample(1420)  # sampled attach transition; the preceding minute remains a violation
healthy_minute = sample(1480)
ok(healthy_minute["eligible_running_minutes"] == 3.0
   and healthy_minute["attached_minutes"] == 1.0,
   "sampled attached time contributes to numerator and denominator")
ok(healthy_minute["watchability"] == round(1 / 3, 6)
   and healthy_minute["by_host"]["host-a"]["watchability"] == round(1 / 3, 6),
   "fleet and per-host watchability use the same running-minute ratio")

os.environ["PM_RUNNER_WATCHABILITY_TARGET"] = "0.995"
tightened = sample(1540)
os.environ["PM_RUNNER_WATCHABILITY_TARGET"] = "0.99"
not_loosened = sample(1600)
ok(tightened["target"] == 0.995 and not_loosened["target"] == 0.995,
   "the configured SLO target may tighten but cannot loosen in-process")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
