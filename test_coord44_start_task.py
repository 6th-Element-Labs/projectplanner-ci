#!/usr/bin/env python3
"""COORD-44 core: one start_task() operation behind every surface.

Three divergent launch paths (UI watch-resolve, autopilot co_fleet wakes,
hand-run server scripts) were the root defect behind BUG-91's residue. This
proves the unified contract:

  live watchable runner  -> attach, never duplicate
  dispatch in flight     -> starting, idempotent (no second wake)
  neither                -> start on the enrolled personal host
  failure                -> one truthful reason + the dispatcher's verdict

and that REST, MCP, and the coordinator all reference the same operation.
"""
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="coord-44-start-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402
import dispatch as dispatch_mod  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def live_bound_runner(task_id, runner_id):
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": "host/steve-mbp-co16",
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "claim_id": f"claim-{runner_id}", "status": "running", "cwd": "/work",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3",
                    "runner_open": True, "runner_inject": True},
        "metadata": {"wake_id": f"wake-{runner_id}",
                     "work_session_id": f"ws-{runner_id}"},
        "heartbeat_at": time.time(), "heartbeat_ttl_s": 180,
    }, actor="coord-44-test", project=P)


def wake_count(task_id):
    return len(store.list_wake_intents(task_id=task_id, project=P,
                                       include_archived=True))


try:
    store.init_db(P)

    # ---- 1) live session -> attach, never a duplicate ----------------------
    attach_task = store.create_task(
        {"workstream_id": "COORD", "title": "attach to the live session"},
        actor="coord-44-test", project=P)["task_id"]
    live_bound_runner(attach_task, "run_live_attach")
    before = wake_count(attach_task)
    result = dispatch_mod.start_task(attach_task, actor="coord-44-test", project=P,
                                     principal_id="user-owner")
    ok(result.get("action") == "attach" and result.get("attached") is True,
       "a live watchable runner attaches instead of starting a duplicate")
    ok(result.get("runner_session_id") == "run_live_attach",
       "attach returns the server-resolved runner, never a caller-chosen one")
    ok(wake_count(attach_task) == before,
       "attaching creates no wake at all")

    # ---- 2) in-flight dispatch -> starting, idempotent ----------------------
    pending_task = store.create_task(
        {"workstream_id": "COORD", "title": "start is idempotent"},
        actor="coord-44-test", project=P)["task_id"]
    pending_wake = store.request_wake(
        {"runtime": "codex", "lane": "COORD", "task_id": pending_task},
        task_id=pending_task, policy={"mode": "direct_task"},
        reason="first click", actor="coord-44-test", project=P)["wake_id"]
    boom = {"called": False}
    saved_dispatch = dispatch_mod.dispatch
    dispatch_mod.dispatch = lambda *a, **k: boom.update(called=True) or {"dispatched": True}
    second = dispatch_mod.start_task(pending_task, actor="coord-44-test", project=P,
                                     principal_id="user-owner")
    dispatch_mod.dispatch = saved_dispatch
    ok(second.get("action") == "starting" and second.get("wake_id") == pending_wake,
       "a second click reports the in-flight start instead of racing a duplicate")
    ok(boom["called"] is False,
       "the idempotency guard refuses to even attempt a second dispatch")

    # ---- 3) nothing live, nothing pending -> start on the personal host ----
    fresh_task = store.create_task(
        {"workstream_id": "COORD", "title": "fresh start"},
        actor="coord-44-test", project=P)["task_id"]
    captured = {}

    def fake_dispatch(task_id, actor="user", project=store.DEFAULT_PROJECT,
                      runtime="", principal_id="", role="implementation", **kwargs):
        captured.update(task_id=task_id, runtime=runtime,
                        principal_id=principal_id, role=role)
        return {"dispatched": True, "wake_id": "wake-fresh",
                "host_id": "host/steve-mbp-co16", "branch": "codex/fresh",
                "execution_mode": "direct_task", "work_hosts_online": 1}

    dispatch_mod.dispatch = fake_dispatch
    started = dispatch_mod.start_task(fresh_task, actor="coord-44-test", project=P,
                                      principal_id="user-owner")
    dispatch_mod.dispatch = saved_dispatch
    ok(started.get("action") == "started" and started.get("started") is True,
       "with nothing live or pending, start_task launches a session")
    ok(captured.get("runtime") == "codex"
       and captured.get("principal_id") == "user-owner",
       "the launch goes down the direct personal-host codex path as the caller")
    ok(started.get("host_id") == "host/steve-mbp-co16",
       "the start reports which host will run the session")

    # ---- 4) failure -> one truthful reason + the dispatcher's verdict -------
    blocked_task = store.create_task(
        {"workstream_id": "COORD", "title": "truthful refusal"},
        actor="coord-44-test", project=P)["task_id"]
    store.complete_wake(
        store.request_wake(
            {"runtime": "codex", "lane": "COORD", "task_id": blocked_task},
            task_id=blocked_task, policy={"mode": "co_fleet"},
            reason="prior auto attempt", actor="coord-44-test", project=P)["wake_id"],
        result={"started": False, "failure_class": "capacity_unavailable",
                "reason": "capacity exhausted for co-general: cap=4"},
        actor="coord-44-test", project=P)
    dispatch_mod.dispatch = lambda *a, **k: {
        "dispatched": False, "error": "personal_agent_host_not_enrolled",
        "reason": "No active Codex Agent Host enrollment belongs to this user."}
    refused = dispatch_mod.start_task(blocked_task, actor="coord-44-test", project=P,
                                      principal_id="user-nobody")
    dispatch_mod.dispatch = saved_dispatch
    ok(refused.get("action") == "refused"
       and refused.get("error") == "personal_agent_host_not_enrolled",
       "a failed start returns the actual blocker, never a stale-runner riddle")
    ok("capacity exhausted for co-general: cap=4"
       in str((refused.get("dispatch") or {}).get("reason") or ""),
       "the refusal carries the dispatcher's own latest verdict for context")

    # ---- 5) every surface references the same operation ---------------------
    # SIMPLIFY-10 moved every surface onto the task_execution command service;
    # dispatch.start_task is now the launcher that only the service calls.
    root = os.path.dirname(os.path.abspath(__file__))
    rest = open(os.path.join(root, "src/switchboard/api/routers/tasks.py")).read()
    mcp = open(os.path.join(root, "src/switchboard/mcp/tools/task_execution.py")).read()
    authz = open(os.path.join(root, "src/switchboard/mcp/authorization.py")).read()
    service = open(os.path.join(
        root, "src/switchboard/application/commands/task_execution.py")).read()
    ok('"/api/tasks/{task_id}/start"' in rest
       and 'task_execution_command.execute_mapping_result' in rest,
       "REST exposes the same start_task operation")
    ok("def start_task" in mcp and '_run("start_task"' in mcp
       and '"start_task"' in mcp,
       "MCP exposes the same start_task operation")
    ok("dispatch_mod.start_task" in service,
       "the service is the one caller of the start_task launcher")
    ok('"start_task"' in authz,
       "MCP authorization admits start_task as a write tool")
    coordinator = open(os.path.join(root, "mission_coordinator.py")).read()
    launcher_source = open(os.path.join(root, "dispatch.py")).read()
    ok("PM_AUTOPILOT_COFLEET" not in coordinator + launcher_source,
       "the retired PM_AUTOPILOT_COFLEET pause no longer exists")

    # ---- 6) placement is command-owned and acceptance-gated -----------------
    ok("def _aws_canary_qualified" in launcher_source
       and 'get_task("DOGFOOD-20"' in launcher_source
       and "use_aws_overflow = bool(aws_qualified and not personal_host_live)" in launcher_source,
       "start_task prefers an enrolled Mac and permits AWS overflow only after DOGFOOD-20")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nCOORD-44 start_task core: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
