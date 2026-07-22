#!/usr/bin/env python3
"""WATCH-6: dark runner alarm is windowed, visible, narrated, and self-clearing."""
from __future__ import annotations

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
    {"runner_session_id": "run-dark", "task_id": "WATCH-6", "status": "running"},
    {"runner_session_id": "run-live", "task_id": "WATCH-7", "status": "ready"},
]
attachments = {"run-dark": False, "run-live": True}
events = []


def sessions(_project):
    return list(rows)


def attached(sid):
    return attachments.get(sid)


def event_sink(project, **payload):
    events.append({"project": project, **payload})


monitor.reset_for_tests()
before = monitor.snapshot(
    "switchboard", sessions_provider=sessions, attachment_provider=attached,
    event_sink=event_sink, now=1000, window_s=300,
)
ok(before["active"] is False and before["count"] == 0,
   "first detached observation starts the trailing window without alarming")

still_before = monitor.snapshot(
    "switchboard", sessions_provider=sessions, attachment_provider=attached,
    event_sink=event_sink, now=1299.9, window_s=300,
)
ok(still_before["active"] is False and not events,
   "a runner detached for less than the threshold stays quiet")

raised = monitor.snapshot(
    "switchboard", sessions_provider=sessions, attachment_provider=attached,
    event_sink=event_sink, now=1300, window_s=300,
)
ok(raised["active"] is True and raised["task_ids"] == ["WATCH-6"],
   "continuous detachment through the threshold raises with affected task IDs")
ok(len(events) == 1 and events[0]["active"] is True
   and events[0]["task_ids"] == ["WATCH-6"],
   "alarm transition emits one narration event naming affected tasks")

monitor.snapshot(
    "switchboard", sessions_provider=sessions, attachment_provider=attached,
    event_sink=event_sink, now=1600, window_s=300,
)
ok(len(events) == 1, "repeated polls do not repeat the narration event")

attachments["run-dark"] = True
cleared = monitor.snapshot(
    "switchboard", sessions_provider=sessions, attachment_provider=attached,
    event_sink=event_sink, now=1601, window_s=300,
)
ok(cleared["active"] is False and len(events) == 2
   and events[-1]["active"] is False
   and events[-1]["task_ids"] == ["WATCH-6"],
   "reattachment immediately clears the alarm and emits a clear transition")

attachments["run-dark"] = False
restarted = monitor.snapshot(
    "switchboard", sessions_provider=sessions, attachment_provider=attached,
    event_sink=event_sink, now=1602, window_s=300,
)
ok(restarted["active"] is False,
   "a later detachment starts a fresh window instead of sticking stale")

attachments["run-dark"] = None
unknown = monitor.snapshot(
    "switchboard", sessions_provider=sessions, attachment_provider=attached,
    event_sink=event_sink, now=2000, window_s=300,
)
ok(unknown["active"] is False,
   "unknown off-process attachment state cannot create a false alarm")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
