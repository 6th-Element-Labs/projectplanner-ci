#!/usr/bin/env python3
"""WATCH-19: a live lease with a silent PTY is a typed progress fault, not health."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from path_setup import ROOT  # noqa: F401

import runner_progress_monitor as monitor  # noqa: E402
from adapters import agent_host  # noqa: E402
from switchboard.application.queries import completion_projection  # noqa: E402
from switchboard.api.routers import attention as attention_router  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {message}")
    else:
        failed += 1
        print(f"  FAIL  {message}")


# ---------------------------------------------------------------------------
# Host: heartbeat carries last_output_at (data the wrapper already has)
# ---------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    log_path = Path(tmp) / "pty.log"
    log_path.write_text("rate limit menu\nwaiting...\n", encoding="utf-8")
    stale_mtime = time.time() - 100
    os.utime(log_path, (stale_mtime, stale_mtime))

    sessions = [{
        "runner_session_id": "run-silent",
        "host_id": "host/test",
        "task_id": "ADAPTER-28",
        "agent_id": "codex/ADAPTER-28",
        "runtime": "codex",
        "pid": 4242,
        "alive": True,
        "stale": False,
        "status": "running",
        "cwd": tmp,
        "log_path": str(log_path),
        "wake_id": "wake-1",
        "wake_mode": "claim_next",
        "started_at": time.time() - 9 * 3600,
        "control": {
            "tier": "T3", "runner_kill": True, "managed_process": True,
            "runner_open": True, "runner_inject": True, "runner_logs": True,
        },
        "metadata": {
            "native_host_execution": True,
            "pty": True,
            "wake_id": "wake-1",
            "log_path": str(log_path),
            "direct_assignment": True,
        },
        "claim_id": "claim-1",
    }]
    bodies = []
    old_drain = agent_host._drain_runners
    old_try = agent_host._try
    old_work = agent_host._drain_work_sessions
    old_preflight = agent_host._host_repo_preflight
    old_relay = agent_host._fresh_server_relay
    old_consume = agent_host._consume_host_relay_refresh_request
    try:
        agent_host._drain_runners = lambda _host_id: list(sessions)
        agent_host._drain_work_sessions = lambda **_kw: [{
            "work_session_id": "ws-1",
            "claim_id": "claim-1",
            "task_id": "ADAPTER-28",
            "agent_id": "codex/ADAPTER-28",
            "status": "active",
        }]
        agent_host._host_repo_preflight = lambda *_a, **_k: None
        agent_host._fresh_server_relay = lambda *_a, **_k: {}
        agent_host._consume_host_relay_refresh_request = lambda *_a, **_k: None

        def capture(method, path, body=None):
            if path == agent_host.P_HEARTBEAT_RUNNER:
                bodies.append(body or {})
            return {"ok": True}

        agent_host._try = capture
        renewed = agent_host.renew_live_direct_runners({"host_id": "host/test"})
    finally:
        agent_host._drain_runners = old_drain
        agent_host._try = old_try
        agent_host._drain_work_sessions = old_work
        agent_host._host_repo_preflight = old_preflight
        agent_host._fresh_server_relay = old_relay
        agent_host._consume_host_relay_refresh_request = old_consume

ok(renewed and renewed[0].get("renewed") is True, "host renews the live runner")
ok(len(bodies) == 1, "exactly one heartbeat is posted")
meta = (bodies[0].get("metadata") or {}) if bodies else {}
ok(isinstance(meta.get("last_output_at"), (int, float))
   and abs(float(meta["last_output_at"]) - stale_mtime) < 2.0,
   "heartbeat reports last_output_at from the PTY log mtime")
ok(int(meta.get("output_bytes") or 0) > 0, "heartbeat reports output_bytes")
ok("log_tail" not in meta,
   "routine renew omits PTY log_tail so Capacity heartbeats stay light")


# ---------------------------------------------------------------------------
# Coordinator monitor: live lease + silent PTY → one typed fault
# ---------------------------------------------------------------------------

events = []


def event_sink(project, **payload):
    events.append({"project": project, **payload})


def silent_row(now, *, last_output_at, output_age=None, status="running",
               stale=False, last_output_present=True):
    meta = {
        "native_host_execution": True,
        "pty": True,
        "log_tail": "provider rate limit\nretry in 60s\n",
        "cpu_percent": 0.0,
    }
    if last_output_present:
        meta["last_output_at"] = last_output_at
        meta["output_bytes"] = 42
    return {
        "runner_session_id": "run-silent",
        "task_id": "ADAPTER-28",
        "host_id": "host/mac",
        "status": status,
        "stale": stale,
        "live": not stale and status == "running",
        "heartbeat_at": now,
        "metadata": meta,
    }


monitor.reset_for_tests()
bound = 1800.0
t0 = 10_000.0
rows = [silent_row(t0, last_output_at=t0 - 100)]

before = monitor.snapshot(
    "switchboard",
    sessions_provider=lambda _p: rows,
    event_sink=event_sink,
    now=t0,
    bound_s=bound,
)
ok(before["active"] is False and before["count"] == 0 and not events,
   "fresh output within the bound does not raise")

silent_at = t0 - 100
rows[0] = silent_row(silent_at + bound - 1, last_output_at=silent_at)
still = monitor.snapshot(
    "switchboard",
    sessions_provider=lambda _p: rows,
    event_sink=event_sink,
    now=silent_at + bound - 1,
    bound_s=bound,
)
ok(still["active"] is False and not events,
   "output age just under the bound stays quiet")

rows[0] = silent_row(silent_at + bound, last_output_at=silent_at)
raised = monitor.snapshot(
    "switchboard",
    sessions_provider=lambda _p: rows,
    event_sink=event_sink,
    now=silent_at + bound,
    bound_s=bound,
)
ok(raised["active"] is True and raised["task_ids"] == ["ADAPTER-28"],
   "live lease with silent PTY past the bound raises")
ok(len(events) == 1 and events[0]["active"] is True,
   "raise emits exactly one narration/attention transition")
fault = (raised.get("faults") or {}).get("run-silent") or {}
ok(fault.get("kind") == "runner_progress_stalled",
   "fault is typed as runner_progress_stalled")
ok(float(fault.get("output_age_s") or 0) >= bound,
   "fault carries output age")
ok(fault.get("cpu_percent") == 0.0, "fault carries CPU when available")
ok("rate limit" in str(fault.get("log_tail") or ""),
   "fault carries PTY log tail evidence")

monitor.snapshot(
    "switchboard",
    sessions_provider=lambda _p: rows,
    event_sink=event_sink,
    now=t0 + bound + 60,
    bound_s=bound,
)
ok(len(events) == 1, "repeated polls while still silent do not re-raise")

# Heartbeat renewal alone must not suppress the fault.
rows[0] = silent_row(t0 + bound + 120, last_output_at=t0 - 100)
rows[0]["heartbeat_at"] = t0 + bound + 120
renewed_still = monitor.snapshot(
    "switchboard",
    sessions_provider=lambda _p: rows,
    event_sink=event_sink,
    now=t0 + bound + 120,
    bound_s=bound,
)
ok(renewed_still["active"] is True and len(events) == 1,
   "heartbeat renewal alone never suppresses the fault")

# Producing output clears / never faults.
rows[0] = silent_row(t0 + bound + 200, last_output_at=t0 + bound + 190)
cleared = monitor.snapshot(
    "switchboard",
    sessions_provider=lambda _p: rows,
    event_sink=event_sink,
    now=t0 + bound + 200,
    bound_s=bound,
)
ok(cleared["active"] is False and len(events) == 2 and events[-1]["active"] is False,
   "fresh PTY output clears the fault with one transition")

# Long-running but productive runners never fault.
events.clear()
monitor.reset_for_tests()
productive = [silent_row(t0 + 9 * 3600, last_output_at=t0 + 9 * 3600 - 30)]
long = monitor.snapshot(
    "switchboard",
    sessions_provider=lambda _p: productive,
    event_sink=event_sink,
    now=t0 + 9 * 3600,
    bound_s=bound,
)
ok(long["active"] is False and not events,
   "a runner producing output never faults regardless of duration")

# Stale/expired leases are not progress faults (liveness owns those).
monitor.reset_for_tests()
events.clear()
stale_rows = [silent_row(t0 + bound, last_output_at=t0 - 100, stale=True)]
stale = monitor.snapshot(
    "switchboard",
    sessions_provider=lambda _p: stale_rows,
    event_sink=event_sink,
    now=t0 + bound,
    bound_s=bound,
)
ok(stale["active"] is False, "expired/stale leases are not progress faults")


# ---------------------------------------------------------------------------
# Session stamp helper: apply_progress_fault mutates metadata once
# ---------------------------------------------------------------------------

session = silent_row(t0 + bound, last_output_at=t0 - 100)
first = monitor.apply_progress_fault(session, now=t0 + bound, bound_s=bound)
ok(first is not None and first.get("kind") == "runner_progress_stalled",
   "apply_progress_fault returns a typed fault for a silent live runner")
ok((session["metadata"].get("progress_fault") or {}).get("kind")
   == "runner_progress_stalled",
   "fault is stamped onto the runner session metadata")
raised_at = session["metadata"]["progress_fault"]["raised_at"]
second = monitor.apply_progress_fault(session, now=t0 + bound + 50, bound_s=bound)
ok(second is not None
   and session["metadata"]["progress_fault"]["raised_at"] == raised_at,
   "re-apply keeps exactly one fault (raised_at stable)")

session["metadata"]["last_output_at"] = t0 + bound + 40
cleared_fault = monitor.apply_progress_fault(
    session, now=t0 + bound + 50, bound_s=bound)
ok(cleared_fault is None and "progress_fault" not in session["metadata"],
   "fresh output clears the stamped progress_fault")


# ---------------------------------------------------------------------------
# Completion projection surfaces blocked_reason from progress_fault
# ---------------------------------------------------------------------------

proj = {"schema": "switchboard.completion_projection.v1", "terminal": False}
faulted = {
    "runner_session_id": "run-silent",
    "task_id": "ADAPTER-28",
    "status": "running",
    "stale": False,
    "metadata": {
        "progress_fault": {
            "kind": "runner_progress_stalled",
            "output_age_s": 1900,
            "log_tail": "stuck at provider prompt",
            "raised_at": t0 + bound,
        }
    },
}
completion_projection._attach_blocked_reason(
    proj, "ADAPTER-28", project="switchboard",
    runner_sessions_provider=lambda **_k: [faulted],
)
ok(proj.get("blocked_reason", "").startswith("runner_progress_stalled"),
   "completion projection exposes progress fault as blocked_reason")
ok(proj.get("blocked_effect") == "run-silent",
   "blocked_effect points at the stalled runner session")


# ---------------------------------------------------------------------------
# Attention feed projects active progress faults
# ---------------------------------------------------------------------------

item = attention_router._runner_progress_item(faulted, now=t0 + bound + 10)
ok(item["source"] == "runner"
   and item["kind"] == "runner_progress_stalled"
   and item["task_id"] == "ADAPTER-28",
   "Attention projects a runner progress fault item")
ok("output_age_s" in (item.get("payload") or {})
   and "stuck at provider prompt" in str((item.get("payload") or {}).get("log_tail") or ""),
   "Attention payload carries output age and log tail")


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
