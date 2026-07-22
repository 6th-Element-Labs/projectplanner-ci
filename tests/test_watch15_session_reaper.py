#!/usr/bin/env python3
"""WATCH-15: one task session ends after completed work or genuine inactivity."""
import os
import tempfile
from pathlib import Path

from path_setup import ROOT  # noqa: F401,E402
from adapters import agent_host  # noqa: E402


def session(log_path, *, runner_id, claim=None, started_at=100.0):
    return {
        "runner_session_id": runner_id,
        "host_id": "host/test",
        "task_id": "WATCH-15",
        "agent_id": "agent/codex/watch-15",
        "claim_id": "claim-1" if claim else "",
        "claim": claim,
        "alive": True,
        "status": "running",
        "started_at": started_at,
        "log_path": str(log_path),
        "metadata": {"wake_id": f"wake-{runner_id}", "log_path": str(log_path)},
    }


def run():
    old_drain = agent_host._drain_runners
    old_action = agent_host.supervisor_action
    old_try = agent_host._try
    old_drop = agent_host._drop_host_bridge
    old_grace = os.environ.get("PM_AGENT_HOST_REAP_GRACE_SECONDS")
    old_idle = os.environ.get("PM_AGENT_HOST_IDLE_TIMEOUT_SECONDS")
    calls = []
    try:
        os.environ["PM_AGENT_HOST_REAP_GRACE_SECONDS"] = "120"
        os.environ["PM_AGENT_HOST_IDLE_TIMEOUT_SECONDS"] = "1800"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logs = {name: root / f"{name}.log" for name in
                    ("active", "recent", "complete", "idle", "chatty")}
            for path in logs.values():
                path.write_text("output", encoding="utf-8")
            now = 10_000.0
            for name, path in logs.items():
                stamp = now - (10 if name in {"recent", "chatty"} else 2_000)
                os.utime(path, (stamp, stamp))
            # BUG-149 guards: a claim the host cannot VERIFY is not a claim that
            # finished. Late-binding (admission preclaim/pending) and a bound
            # claim_id whose row didn't drain must both be left alone.
            late_bind = session(logs["complete"], runner_id="latebind",
                                claim={"status": "completed",
                                       "completed_at": now - 500})
            late_bind["metadata"]["credential_admission_phase"] = "preclaim"
            skewed = session(logs["idle"], runner_id="skewed")
            skewed["claim_id"] = "claim-skewed"
            rows = [
                session(logs["active"], runner_id="active",
                        claim={"status": "active", "updated_at": 1}),
                session(logs["recent"], runner_id="recent",
                        claim={"status": "completed", "completed_at": now - 500}),
                session(logs["complete"], runner_id="complete",
                        claim={"status": "completed", "completed_at": now - 500}),
                session(logs["idle"], runner_id="idle"),
                session(logs["chatty"], runner_id="chatty"),
                late_bind,
                skewed,
            ]
            agent_host._drain_runners = lambda host_id: rows
            agent_host.supervisor_action = lambda action, runner_id, options=None: (
                calls.append(("supervisor", action, runner_id, options))
                or {"alive": False, "status": "killed"})
            agent_host._drop_host_bridge = lambda runner_id: calls.append(
                ("drop", runner_id))
            agent_host._try = lambda method, path, body=None: (
                calls.append((method, path, body)) or {"ok": True})

            outcomes = agent_host.reap_finished_or_idle_runners(
                {"host_id": "host/test"}, now=now)

        assert {row["runner_session_id"] for row in outcomes} == {"complete", "idle"}
        assert {row["reason"] for row in outcomes} == {"claim_completed", "idle_timeout"}
        assert all(row["reaped"] and row["wake_completed"] for row in outcomes)
        killed = {call[2] for call in calls if call[:2] == ("supervisor", "kill")}
        assert killed == {"complete", "idle"}
        heartbeats = [call[2] for call in calls
                      if call[:2] == ("POST", agent_host.P_HEARTBEAT_RUNNER)]
        assert {body["metadata"]["reaped_reason"] for body in heartbeats} == {
            "claim_completed", "idle_timeout"}
        assert all(body["metadata"]["terminalized_by"] == "session_reaper"
                   for body in heartbeats)
    finally:
        agent_host._drain_runners = old_drain
        agent_host.supervisor_action = old_action
        agent_host._try = old_try
        agent_host._drop_host_bridge = old_drop
        for key, value in (("PM_AGENT_HOST_REAP_GRACE_SECONDS", old_grace),
                           ("PM_AGENT_HOST_IDLE_TIMEOUT_SECONDS", old_idle)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    run()
    print("WATCH-15 session reaper tests passed")
