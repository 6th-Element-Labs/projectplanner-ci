#!/usr/bin/env python3
"""DISPATCH-9 — the UI/MCP dispatch enqueues a project-aware, lane-scoped wake (not the old
Maxwell/ActionEngine push-bridge). Regression guard for the bug where dispatch dropped `project`
so every non-Maxwell task 404'd and nothing spun up."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dispatch-wake-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_AUTH_MODE"] = "off"

import store  # noqa: E402
import dispatch  # noqa: E402

store.init_db("switchboard")
task = store.create_task(
    {"workstream_id": "HARDEN", "workstream_name": "Harden", "title": "Cheap health probe", "phase": "Build"},
    actor="test", project="switchboard",
)
TID = task["task_id"]

passed = failed = 0


def ok(cond, msg):
    global passed, failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    passed += 1 if cond else 0
    failed += 0 if cond else 1


# 1. status() is always wired now (no external runner required), mode = wake.
st = dispatch.status(project="switchboard")
ok(st.get("configured") is True and st.get("mode") == "wake", "status(): configured + wake mode")

# 2. Dispatching a real switchboard task enqueues a wake (the whole point).
res = dispatch.dispatch(TID, actor="tester", project="switchboard")
ok(res.get("dispatched") is True, "dispatch(): switchboard task dispatched")
ok(bool(res.get("wake_id")), "dispatch(): returns a wake_id")
ok(res.get("lane") == "HARDEN", "dispatch(): wake carries the task's lane")

# 3. The wake lands on the SWITCHBOARD board (the old bug put it on maxwell / 404'd).
sw_wakes = [w for w in store.list_wake_intents(project="switchboard") if w.get("task_id") == TID]
ok(len(sw_wakes) == 1, "wake is recorded on the switchboard board")
sel = (sw_wakes[0].get("selector") or {}) if sw_wakes else {}
ok(sel.get("runtime") == "claude-code" and sel.get("lane") == "HARDEN"
   and "vendor_cloud" in (sel.get("capabilities") or []),
   "wake selector = claude-code vendor cloud + HARDEN lane")
pol = (sw_wakes[0].get("policy") or {}) if sw_wakes else {}
ok(pol.get("mode") == "vendor_cloud" and pol.get("provider") == "anthropic",
   "wake policy asks for Anthropic-hosted execution")

# 4. A non-existent task fails cleanly as 'task not found' (no silent no-op, no wrong-board hit).
miss = dispatch.dispatch("NOPE-999", actor="tester", project="switchboard")
ok(miss.get("dispatched") is False and miss.get("error") == "task not found", "missing task → task not found")

# 5. latest() reflects the queued state for the Dev-tab panel.
latest = dispatch.latest(TID, project="switchboard")
ok(latest.get("status") == "queued", f"latest(): status queued (got {latest.get('status')})")
ok(latest.get("wake_id") == res.get("wake_id"), "latest(): surfaces the same wake_id")

# 6. work_hosts_online reflects a REAL registered host. allow_work is advertised
#    per-runtime under runtimes[].policy (the shape register_host persists) — a
#    detector that only reads a top-level allow_work would always report 0 online
#    and the UI would falsely say "no work host is online" even with one running.
ok(dispatch.status(project="switchboard").get("work_hosts_online") == 0,
   "work_hosts_online = 0 before any host registers")
msg_only = store.register_host(
    {"host_id": "host/msg-only", "hostname": "msg-only",
     "runtimes": [{"runtime": "claude-code", "policy": {"allow_work": False, "mode": "message_only"},
                   "lanes": [], "capabilities": ["python"]}],
     "limits": {"max_sessions": 1}, "heartbeat_ttl_s": 60},
    project="switchboard")
ok(not msg_only.get("error"), "registered a message-only host")
ok(dispatch.status(project="switchboard").get("work_hosts_online") == 0,
   "message-only host is NOT counted as work-capable")
work_host = store.register_host(
    {"host_id": "host/worker", "hostname": "worker",
     "runtimes": [{"runtime": "claude-code", "policy": {"allow_work": True, "mode": "lane_scoped"},
                   "lanes": [], "capabilities": ["python", "github"]}],
     "limits": {"max_sessions": 2}, "heartbeat_ttl_s": 60},
    project="switchboard")
ok(not work_host.get("error"), "registered a work-capable host")
ok(dispatch._host_is_work_capable(work_host) is True,
   "_host_is_work_capable() reads runtimes[].policy.allow_work")
ok(dispatch.status(project="switchboard").get("work_hosts_online") == 0,
   "local work host is not mislabeled as a cloud trigger host")
cloud_host = store.register_host(
    {"host_id": "host/cloud", "hostname": "cloud-trigger",
     "runtimes": [{"runtime": "claude-code",
                   "policy": {"allow_work": True, "mode": "vendor_cloud_trigger"},
                   "lanes": ["HARDEN"],
                   "capabilities": ["vendor_cloud", "github", "mcp"]}],
     "limits": {"max_sessions": 4}, "heartbeat_ttl_s": 60},
    project="switchboard")
ok(not cloud_host.get("error"), "registered a Claude cloud trigger host")
ok(dispatch.status(project="switchboard").get("work_hosts_online") == 1,
   "work_hosts_online counts only the vendor-cloud trigger host")
# and the dispatch note no longer warns about a missing host
res2 = dispatch.dispatch(TID, actor="tester", project="switchboard")
ok(res2.get("work_hosts_online") == 1, "dispatch() reports the online work host")

# 7. An adopted provider session exposes its app URL in the Dev-tab projection.
wake_id = res.get("wake_id")
runner_id = "cloud/claude-code-cloud/cse_dispatch18"
store.complete_wake(
    wake_id,
    runner_session_id=runner_id,
    agent_id=f"claude/{TID}",
    result={
        "started": True,
        "vendor_id": "claude-code-cloud",
        "provider_session_id": "cse_dispatch18",
        "session_url": "https://claude.ai/code/session_dispatch18",
        "billing_mode": "subscription",
        "heartbeat_ttl_s": 86400,
        "control": {"provider_app": True, "runner_kill": False,
                    "managed_process": False},
    },
    actor="test",
    project="switchboard",
)
bound = dispatch.latest(TID, project="switchboard")
ok(bound.get("session_url") == "https://claude.ai/code/session_dispatch18"
   and bound.get("provider_session_id") == "cse_dispatch18",
   "latest() exposes the app-visible Claude session binding")
bound_runner = store.get_runner_session(runner_id, project="switchboard")
ok((bound_runner.get("metadata") or {}).get("vendor_id") == "claude-code-cloud"
   and (bound_runner.get("metadata") or {}).get("billing_mode") == "subscription"
   and (bound_runner.get("control") or {}).get("runner_kill") is False
   and bound_runner.get("heartbeat_ttl_s") == 86400,
   "cloud runner keeps provider metadata/lifetime and does not advertise a fake local kill")

# 8. latest() returns the NEWEST wake, not the oldest. Two distinct wakes for one
#    task: list_wake_intents returns them oldest-first, so a created_at (absent on
#    wake rows) sort key would wrongly pin latest() to the first/oldest.
store.request_wake(selector={"runtime": "claude-code", "lane": "HARDEN"},
                   reason="older", source="test", policy={"mode": "claim_next"},
                   task_id=TID, actor="test", project="switchboard",
                   idem_key="dispatch-test-old")
store.request_wake(selector={"runtime": "claude-code", "lane": "HARDEN"},
                   reason="newer", source="test", policy={"mode": "claim_next"},
                   task_id=TID, actor="test", project="switchboard",
                   idem_key="dispatch-test-new")
multi = [w for w in store.list_wake_intents(project="switchboard") if w.get("task_id") == TID]
ok(len(multi) >= 2, f"task has multiple distinct wakes ({len(multi)})")
newest_id = max(multi, key=lambda w: w.get("requested_at") or 0).get("wake_id")
ok(dispatch.latest(TID, project="switchboard").get("wake_id") == newest_id,
   "latest() surfaces the newest wake (requested_at), not the oldest")

print(f"\nDispatch wake: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
