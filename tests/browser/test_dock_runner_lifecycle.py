#!/usr/bin/env python3
"""Runner lifecycle: finished is not broken, and finished runners do not linger.

The dock collapsed every non-running runner into a red "Exited", so a runner
that completed its task looked identical to one that crashed and counted toward
the operator's attention total. Terminal runners also stayed on screen forever.

Retention rule (option B): a clean exit is a receipt — show it briefly, then
drop it. A failure is not swept away on a timer; it clears when a newer runner
for the same task supersedes it.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SCRIPTS = [
    "static/js/api.js", "static/js/state.js", "static/js/board.js",
    "static/js/plan-chat.js", "static/js/mission.js", "static/js/runner-session.js",
    "static/js/proof-console.js", "static/js/project-admin.js", "static/js/settings.js",
    "static/js/scope.js", "static/js/attention.js", "static/js/fleet-dock.js",
    "static/app.js",
]

failures = []


def ok(condition, message):
    if not condition:
        failures.append(message)


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_content('<div id="fleet-dock"></div>')
    for rel in SCRIPTS:
        page.add_script_tag(path=str(ROOT / rel))

    def outcome(session):
        return page.evaluate(
            "(s) => window.SwitchboardFleetDock.runnerOutcome(s)", session)

    # --- each terminal status reads as what it actually was -----------------
    fin = outcome({"status": "completed"})
    ok(fin["key"] == "finished" and fin["tone"] == "green",
       f"a completed runner is Finished, not broken: {fin}")

    killed = outcome({"status": "killed"})
    ok(killed["key"] == "stopped" and killed["tone"] != "red",
       f"a killed runner reads as stopped by you: {killed}")

    failed = outcome({"status": "failed", "result": {"failure_class": "launch_failed"}})
    ok(failed["key"] == "failed" and failed["tone"] == "red",
       f"a failed runner is red: {failed}")
    ok("launch_failed" in failed["label"],
       f"the server's failure_class must reach the badge: {failed}")

    unknown = outcome({"status": "weird"})
    ok(unknown["key"] == "ended_unknown" and unknown["tone"] == "yellow",
       f"an unrecognised terminal status is not claimed as a clean finish: {unknown}")

    # Real prod distribution (602 sessions): expired is 238 of them — the
    # lifecycle sweep reaping old rows. It is housekeeping, and flagging every
    # one would bury the ~106 that genuinely failed.
    expired = outcome({"status": "expired"})
    ok(expired["key"] == "expired" and expired["tone"] != "red",
       f"a reaped session is housekeeping, not an alarm: {expired}")

    # 'exited' is the supervisor finding the process gone with no completion
    # report — not provably a failure, definitely not a proven success.
    exited = outcome({"status": "exited"})
    ok(exited["key"] == "exited_unexpectedly" and exited["tone"] == "yellow",
       f"a vanished process is not reported as a clean finish: {exited}")

    # --- retention ---------------------------------------------------------
    def retention(session, key, others=None, now_ms=1_000_000_000_000):
        return page.evaluate(
            "([s, k, all, now]) => window.SwitchboardFleetDock.runnerRetention(s, k, all, now)",
            [session, key, others or [], now_ms])

    now_s = 1_000_000_000
    now_ms = now_s * 1000

    fresh = {"status": "completed", "updated_at": now_s - 30, "task_id": "T-1"}
    ok(retention(fresh, "finished", [fresh], now_ms) == "keep",
       "a just-finished runner stays briefly as a receipt")

    old = {"status": "completed", "updated_at": now_s - 600, "task_id": "T-1"}
    ok(retention(old, "finished", [old], now_ms) == "drop",
       "a clean exit ages out instead of staying forever")

    no_ts = {"status": "completed", "updated_at": 0, "task_id": "T-1"}
    ok(retention(no_ts, "finished", [no_ts], now_ms) == "keep",
       "an unknown end time is kept, never silently dropped")

    # A failure is NOT aged out, however old — that is the whole point.
    stale_fail = {"status": "failed", "updated_at": now_s - 86400, "task_id": "T-2"}
    ok(retention(stale_fail, "failed", [stale_fail], now_ms) == "keep",
       "a day-old failure is still shown; failures do not expire on a timer")

    # ...but it clears once a newer runner for the same task supersedes it.
    newer = {"status": "running", "updated_at": now_s - 10, "task_id": "T-2"}
    ok(retention(stale_fail, "failed", [stale_fail, newer], now_ms) == "drop",
       "a superseded failure clears itself with no operator action")

    other_task = {"status": "running", "updated_at": now_s, "task_id": "T-9"}
    ok(retention(stale_fail, "failed", [stale_fail, other_task], now_ms) == "keep",
       "an unrelated task's runner must not clear this failure")

    live = {"status": "running", "updated_at": now_s, "task_id": "T-3"}
    ok(retention(live, "working", [live], now_ms) == "live",
       "a running runner is never subject to retention")

    # --- the dock renders the buckets and counts accordingly ---------------
    # Ages are relative to the browser's own clock, which is what the dock reads.
    text = page.evaluate(
        """() => {
            const now = Math.floor(Date.now() / 1000);
            const runners = [
                {runner_session_id: 'a', task_id: 'T-1', status: 'completed',
                 updated_at: now - 30, stale: false},
                {runner_session_id: 'b', task_id: 'T-2', status: 'failed',
                 updated_at: now - 60, stale: false,
                 result: {failure_class: 'launch_failed'}},
                {runner_session_id: 'c', task_id: 'T-3', status: 'running',
                 updated_at: now, stale: false},
                {runner_session_id: 'd', task_id: 'T-4', status: 'completed',
                 updated_at: now - 9999, stale: false},
            ];
            const host = document.getElementById('fleet-dock');
            const ctx = Object.create(TeepPlan);
            ctx._dockCollapsed = false;
            ctx._dockAttention = {};
            ctx._fleetTaskTitle = () => '';
            ctx._renderFleetDock = () => {};
            host.innerHTML = '';
            TeepPlan._renderFleetDock.call(ctx, runners, [],
                {production: {last_deploy_ok: true}, deployments: []});
            return host.innerText;
        }""")

    ok("Recently finished" in text, f"finished work gets its own bucket: {text!r}")
    ok("Failed" in text, f"failures are named Failed, not Broken: {text!r}")
    ok("Broken" not in text, f"nothing is called Broken any more: {text!r}")
    # One failure needs the operator; the completed runners must not count.
    ok("1 needs you" in text,
       f"only the failure counts toward attention, got: {text!r}")
    # The long-finished runner aged out; the fresh one did not.
    ok("T-4" not in text, f"a long-finished runner is gone: {text!r}")
    ok("T-1" in text, f"a just-finished runner is still shown: {text!r}")

    ok(errors == [], f"no console errors: {errors}")
    browser.close()

if failures:
    for line in failures:
        print("FAIL", line)
    raise SystemExit(1)
print("PASS test_dock_runner_lifecycle")
