#!/usr/bin/env python3
"""SIMPLIFY-10: the complete Task Session command set, and its one authority.

COORD-44 shipped ``start_task`` alone. This proves the remaining six commands,
that REST and MCP answer identically for every one of them, that retry
supersedes an attempt instead of forking a second execution, and that the
execution-authority ratchet holds.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="simplify-10-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

from path_setup import ROOT  # noqa: E402

import store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from switchboard.application.commands import task_execution  # noqa: E402
from switchboard.mcp import authorization as mcp_authorization  # noqa: E402
from switchboard.mcp.tools import task_execution as mcp_task_execution  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def live_runner(task_id, runner_id, status="running"):
    return store.upsert_runner_session({
        "runner_session_id": runner_id, "host_id": "host/steve-mbp-co16",
        "agent_id": f"codex/{task_id}", "runtime": "codex", "task_id": task_id,
        "claim_id": f"claim-{runner_id}", "status": status, "cwd": "/work",
        "control": {"managed_process": True, "runner_kill": True, "tier": "T3",
                    "runner_open": True, "runner_inject": True},
        "metadata": {"wake_id": f"wake-{runner_id}",
                     "work_session_id": f"ws-{runner_id}",
                     "log_tail": "compiling…\nall tests pass\n",
                     "transcript_ref": "https://vendor.example/session/1"},
        "heartbeat_at": time.time(), "heartbeat_ttl_s": 180,
    }, actor="simplify-10-test", project=P)


def new_task(title):
    return store.create_task({"workstream_id": "SIMPLIFY", "title": title},
                             actor="simplify-10-test", project=P)["task_id"]


def live_wake(task_id):
    return store.request_wake(
        {"runtime": "codex", "agent_id": f"codex/{task_id}", "task_id": task_id},
        reason="simplify-10 fixture", source="simplify-10-test",
        task_id=task_id, actor="simplify-10-test", project=P,
        idem_key=f"s10-{task_id}", policy={"mode": "direct_cli"})


def expand_routes(routes):
    """Flatten FastAPI's lazy _IncludedRouter entries for inspection."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from expand_routes(included.routes)
        else:
            yield route


class _StubMcp:
    """Minimal FastMCP stand-in: the tool decorator is all registration needs."""

    def tool(self):
        return lambda function: function


#: Read-time clocks. Two transports serving the same request microseconds apart
#: differ here by construction; everything else must match exactly.
VOLATILE_KEYS = ("generated_at", "checked_at", "uptime_seconds")


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()
                if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def describe_difference(left, right, path="") -> list[str]:
    """Name exactly where two transport bodies diverge, not just that they do."""
    if isinstance(left, dict) and isinstance(right, dict):
        out = []
        for key in sorted(set(left) | set(right)):
            if key not in left:
                out.append(f"only in MCP: {path}/{key}")
            elif key not in right:
                out.append(f"only in REST: {path}/{key}")
            else:
                out.extend(describe_difference(left[key], right[key], f"{path}/{key}"))
        return out
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"length differs at {path}: REST={len(left)} MCP={len(right)}"]
        out = []
        for index, (one, other) in enumerate(zip(left, right)):
            out.extend(describe_difference(one, other, f"{path}[{index}]"))
        return out
    if left != right:
        return [f"{path}: REST={left!r:.80} MCP={right!r:.80}"]
    return []


try:
    store.init_db(P)
    from app import app  # noqa: E402
    client = TestClient(app)

    # Register the MCP adapter against stub edge services so parity is proved at
    # the real tool functions, not just at the command they share.
    mcp_task_execution.register_task_execution_tools(
        _StubMcp(),
        mcp_task_execution.TaskExecutionToolServices(
            dumps=lambda value: json.dumps(value, sort_keys=True, default=str),
            require_write=lambda ctx, project, scopes=None: {
                "id": "user-simplify10", "display_name": "web"},
        ),
    )

    # ---- 1) the command set exists on both transports ----------------------
    expected = {"get_task_execution", "start_task", "open_session", "send_message",
                "stop_task", "retry_task", "get_execution_transcript"}
    ok(set(task_execution.COMMANDS) == expected,
       "the service declares all seven COORD-44 commands")
    ok(set(mcp_task_execution.TASK_EXECUTION_TOOL_NAMES) == expected,
       "MCP registers a tool for every command")
    routes = {(method, route.path)
              for route in expand_routes(app.routes) if hasattr(route, "path")
              for method in (getattr(route, "methods", None) or [])}
    for method, path in (
        ("GET", "/api/tasks/{task_id}/execution"),
        ("POST", "/api/tasks/{task_id}/execution/open"),
        ("POST", "/api/tasks/{task_id}/execution/message"),
        ("POST", "/api/tasks/{task_id}/execution/stop"),
        ("POST", "/api/tasks/{task_id}/execution/retry"),
        ("GET", "/api/tasks/{task_id}/execution/transcript"),
        ("POST", "/api/tasks/{task_id}/start"),
    ):
        ok((method, path) in routes, f"REST exposes {method} {path}")
    census = mcp_authorization.READ_TOOLS | mcp_authorization.WRITE_TOOLS
    ok(expected <= census, "every command is declared in the MCP access census")
    ok({"get_task_execution", "get_execution_transcript"}
       <= mcp_authorization.READ_TOOLS,
       "the two read commands need no write scope")
    ok({"start_task", "open_session", "send_message", "stop_task", "retry_task"}
       <= mcp_authorization.WRITE_TOOLS,
       "the five mutating commands require a write scope")

    # ---- 2) REST and MCP return byte-identical bodies -----------------------
    running_task = new_task("running session")
    live_runner(running_task, "run_s10_live")
    idle_task = new_task("nothing running")

    def rest_json(method, path, **kwargs):
        response = getattr(client, method)(path, **kwargs)
        return response.status_code, response.json()

    def mcp_json(raw):
        return json.loads(raw)

    equivalence = [
        ("get_task_execution", running_task,
         lambda t: rest_json("get", f"/api/tasks/{t}/execution", params={"project": P}),
         lambda t: mcp_json(mcp_task_execution.get_task_execution(t, project=P))),
        ("get_execution_transcript", running_task,
         lambda t: rest_json("get", f"/api/tasks/{t}/execution/transcript",
                             params={"project": P}),
         lambda t: mcp_json(mcp_task_execution.get_execution_transcript(t, project=P))),
        ("get_task_execution/not_found", "SIMPLIFY-NOPE",
         lambda t: rest_json("get", f"/api/tasks/{t}/execution", params={"project": P}),
         lambda t: mcp_json(mcp_task_execution.get_task_execution(t, project=P))),
        ("get_execution_transcript/no_execution", idle_task,
         lambda t: rest_json("get", f"/api/tasks/{t}/execution/transcript",
                             params={"project": P}),
         lambda t: mcp_json(mcp_task_execution.get_execution_transcript(t, project=P))),
    ]
    for name, target, rest_call, mcp_call in equivalence:
        status, rest_body = rest_call(target)
        # Raw body parity: refusals must NOT be wrapped under FastAPI ``detail``.
        ok("detail" not in rest_body or status < 400,
           f"REST {name} returns the typed envelope at the top level "
           f"(no detail wrap; status={status})")
        rest_payload = stable(rest_body)
        mcp_body = stable(mcp_call(target))
        ok(rest_payload == mcp_body, f"REST and MCP agree on {name}")
        for line in describe_difference(rest_payload, mcp_body):
            print(f"         {line}")

    # The mutating commands are proved on their refusals, which are reachable
    # without a live Agent Host and are exactly where two transports usually
    # drift apart (one returns a string, the other an envelope).
    for name, path, call in (
        ("stop_task", "stop",
         lambda t: mcp_json(mcp_task_execution.stop_task(t, None, project=P))),
        ("send_message", "message",
         lambda t: mcp_json(mcp_task_execution.send_message(t, "hi", None, project=P))),
        ("open_session", "open",
         lambda t: mcp_json(mcp_task_execution.open_session(t, None, project=P))),
        ("retry_task", "retry",
         lambda t: mcp_json(mcp_task_execution.retry_task(t, None, project=P))),
    ):
        payload = {"project": P}
        if name == "send_message":
            payload["text"] = "hi"
        status, rest_body = rest_json(
            "post", f"/api/tasks/SIMPLIFY-NOPE/execution/{path}", json=payload)
        mcp_body = call("SIMPLIFY-NOPE")
        ok("detail" not in rest_body,
           f"REST {name} refusal is top-level (not nested under detail)")
        ok(rest_body == mcp_body,
           f"REST and MCP agree on the {name} refusal")
        ok(status == task_execution.ERROR_STATUS.get(mcp_body.get("error_code")),
           f"{name} maps its typed error to the declared HTTP status ({status})")

    # ---- 3) typed errors: one code, one failure_class, one status ----------
    _, missing = rest_json("get", "/api/tasks/SIMPLIFY-NOPE/execution",
                           params={"project": P})
    ok(missing.get("error_code") == "task_not_found"
       and missing.get("failure_class") == "missing_data"
       and missing.get("schema") == task_execution.ERROR_SCHEMA
       and "detail" not in missing,
       "an unknown task refuses with the typed task_not_found envelope")
    empty = task_execution.execute_mapping_result(
        "send_message", running_task, project=P, text="   ")
    ok(empty.get("error_code") == "invalid_input",
       "an empty message is rejected before it reaches a host")
    ok(set(task_execution.ERROR_FAILURE_CLASS) == set(task_execution.ERROR_STATUS),
       "every error code has both an HTTP status and a fail_fix failure_class")

    # send_message must queue a host-accepted inject kind (session_chat).
    from adapters.codex.pty_stream import INJECT_KINDS
    sent = task_execution.send_message(
        running_task, "hello from simplify-10", project=P, actor="simplify-10-test")
    injects = [row for row in store.list_runner_control_requests(
        runner_session_id="run_s10_live", project=P)
        if row.get("action") == "inject"]
    ok(sent.get("queued") is True and injects,
       "send_message queues a durable inject against the live execution")
    inject_kind = str((injects[-1].get("options") or {}).get("kind") or "")
    ok(inject_kind == "session_chat" and inject_kind in INJECT_KINDS,
       f"queued inject kind is host-accepted session_chat (got {inject_kind!r})")

    runner_js = (ROOT / "static/js/runner-session.js").read_text(encoding="utf-8")
    ok("_runnerPtyApiError" in runner_js
       and "JSON.stringify(value)" in runner_js
       and "error_code" in runner_js
       and "execution/message" in runner_js,
       "Watch/Chat durable fallback renders typed API errors (not [object Object])")

    # ---- 4) get_task_execution answers "what is running" -------------------
    view = task_execution.get_task_execution(running_task, project=P)
    ok(view["running"] is True and view["execution_id"] == "run_s10_live",
       "a live runner is reported as running with its execution id")
    ok("stop_task" in view["available_commands"]
       and "send_message" in view["available_commands"],
       "stop and message are offered while a session is live")
    idle_view = task_execution.get_task_execution(idle_task, project=P)
    ok(idle_view["running"] is False and idle_view["execution_id"] is None,
       "a task with no session reports no execution")
    ok("start_task" in idle_view["available_commands"],
       "start is offered when nothing is running")

    # ---- 5) stop ends BOTH halves of the lifecycle -------------------------
    stop_task_id = new_task("stop both halves")
    live_runner(stop_task_id, "run_s10_stop")
    wake = live_wake(stop_task_id)
    stopped = task_execution.stop_task(stop_task_id, project=P, actor="simplify-10-test")
    ok(stopped["killed"] is True and stopped["execution_id"] == "run_s10_stop",
       "stop kills the live runner")
    ok(stopped["cancelled_wake_id"] == wake["wake_id"],
       "stop also cancels the queued start, so the task cannot restart itself")
    after = store.list_wake_intents(task_id=stop_task_id, project=P)
    ok(all(row.get("status") == "cancelled" for row in after),
       "no wake for this task is left in flight after stop")
    not_running = task_execution.execute_mapping_result(
        "stop_task", idle_task, project=P, actor="simplify-10-test")
    ok(not_running.get("error_code") == "not_running",
       "stopping an idle task refuses truthfully instead of reporting success")

    # ---- 6) retry supersedes; it never forks a second execution ------------
    launched = []

    def fake_launcher(task_id, **kwargs):
        launched.append(task_id)
        return {"action": "started", "started": True, "wake_id": "wake-new",
                "host_id": "host/steve-mbp-co16"}

    queued_task = new_task("retry replaces a queued start")
    queued_wake = live_wake(queued_task)
    retried = task_execution.retry_task(queued_task, project=P, actor="simplify-10-test",
                                        launcher=fake_launcher)
    ok(retried["superseded_wake_id"] == queued_wake["wake_id"],
       "retry cancels the queued attempt it is replacing")
    ok(retried["action"] == "started" and launched == [queued_task],
       "retry then launches exactly one replacement")
    remaining = [row for row in store.list_wake_intents(task_id=queued_task, project=P)
                 if row.get("status") in {"pending", "claimed"}]
    ok(len(remaining) == 0,
       "the superseded wake is terminal — two attempts never coexist")

    live_task = new_task("retry never forks past a live runner")
    live_runner(live_task, "run_s10_retry")
    launched.clear()
    superseding = task_execution.retry_task(live_task, project=P,
                                            actor="simplify-10-test",
                                            launcher=fake_launcher)
    ok(superseding["action"] == "superseding" and superseding["started"] is False,
       "retry against a LIVE runner reports superseding rather than starting")
    ok(launched == [],
       "no second session is launched beside a live one — the fork is impossible")
    ok(superseding["superseded_execution_id"] == "run_s10_retry",
       "the retry names the execution it stopped")

    # ---- 7) open_session: the server names the execution, every time -------
    open_task = new_task("open resolves the execution server-side")
    live_runner(open_task, "run_s10_open")
    first = task_execution.execute_mapping_result(
        "open_session", open_task, project=P, actor="simplify-10-test")
    second = task_execution.execute_mapping_result(
        "open_session", open_task, project=P, actor="simplify-10-test")
    ok(first.get("execution_id") == "run_s10_open",
       "open_session resolves the current execution without being told one")
    opens = [row for row in store.list_runner_control_requests(
        runner_session_id="run_s10_open", project=P)
        if row.get("action") == "open"]
    operation_ids = [str((row.get("options") or {}).get("client_request_id") or "")
                     for row in opens]
    ok(len(opens) == 2 and all(operation_ids) and len(set(operation_ids)) == 2,
       "every reconnect issues a FRESH host-open recovery operation, so an "
       "earlier verified open cannot suppress the one a restart needs")
    ok(second.get("error_code") is None,
       "a second open is a legal reconnect, not a refusal")

    # A runner that cannot be told to re-open its tunnel is still watchable over
    # an already-attached one. Denying the relay here would be a regression, so
    # the refusal is reported beside a live ticket instead of replacing it.
    no_open_task = new_task("watchable without runner_open")
    store.upsert_runner_session({
        "runner_session_id": "run_s10_noopen", "host_id": "host/steve-mbp-co16",
        "agent_id": f"codex/{no_open_task}", "runtime": "codex",
        "task_id": no_open_task, "claim_id": "claim-noopen", "status": "running",
        "control": {"managed_process": True, "tier": "T3"},
        "metadata": {"wake_id": "wake-noopen", "work_session_id": "ws-noopen"},
        "heartbeat_at": time.time(), "heartbeat_ttl_s": 180,
    }, actor="simplify-10-test", project=P)
    degraded = task_execution.execute_mapping_result(
        "open_session", no_open_task, project=P, actor="simplify-10-test")
    ok(degraded.get("error_code") is None and degraded.get("ticket"),
       "a runner without runner_open still gets a relay ticket")
    ok(degraded.get("opened") is False and degraded["host_open"]["reason"],
       "the host-tunnel refusal is named, not swallowed into a silent success")

    # ---- 8) transcript is honest about what it has ------------------------
    transcript = task_execution.get_execution_transcript(running_task, project=P)
    ok(transcript["execution_id"] == "run_s10_live"
       and transcript["schema"] == "switchboard.execution_transcript.v1",
       "the transcript resolves the task's execution")
    ok(transcript["complete"] is False and transcript["incomplete_reason"],
       "the transcript never presents a log tail as the full stream")
    ok(any("all tests pass" in segment["text"] for segment in transcript["segments"]),
       "the host log tail is carried as a transcript segment")
    ok(transcript["transcript_ref"] == "https://vendor.example/session/1",
       "the vendor transcript pointer is preserved")
    by_execution = task_execution.get_execution_transcript(
        execution_id="run_s10_live", project=P)
    ok(by_execution["task_id"] == running_task,
       "a transcript can be fetched by execution_id alone")
    mismatched = task_execution.execute_mapping_result(
        "get_execution_transcript", idle_task, execution_id="run_s10_live", project=P)
    ok(mismatched.get("error_code") == "wrong_session",
       "an execution_id from another task is refused, not silently served")

    # ---- 9) the ratchet: one execution authority --------------------------
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "simplify10_execution_authority.py"),
         "--json"],
        cwd=str(ROOT), capture_output=True, text=True, check=False)
    ok(proc.returncode == 0,
       f"execution-authority ratchet exit 0 (code={proc.returncode})")
    report = {}
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        ok(False, f"ratchet JSON parse failed: {(proc.stdout or proc.stderr)[:300]}")
    else:
        ok(report.get("ok") is True, "no scope exceeds its ceiling")
        scopes = report.get("scopes") or {}
        for name in ("wake_assembly_outside_service", "host_selection_outside_service",
                     "assignment_authoring_outside_service",
                     "runner_resolution_outside_service", "browser_execution_facts",
                     "task_execution_surface"):
            ok(name in scopes, f"ratchet measures {name}")
        surface = scopes.get("task_execution_surface") or {}
        ok(surface.get("measured") == 0,
           "the task-execution surface assembles no wake, picks no host, "
           "resolves no runner id")
        for name in ("host_selection_outside_service",
                     "assignment_authoring_outside_service"):
            ok((scopes.get(name) or {}).get("ceiling") == 0,
               f"{name} is a hard zero, not a tolerated count")

    # ---- 10) no client can name an execution on the mutating commands ------
    from switchboard.api.routers import tasks as tasks_router
    body_fields = set(tasks_router.ExecutionCommandBody.model_fields)
    ok(not (body_fields & {"execution_id", "runner_session_id", "host_id", "wake_id"}),
       "the REST command body cannot carry a caller-chosen execution identity")
    import inspect
    for name in ("start_task", "open_session", "send_message", "stop_task", "retry_task"):
        params = set(inspect.signature(
            getattr(mcp_task_execution, name)).parameters)
        ok(not (params & {"runner_session_id", "host_id", "wake_id", "execution_id"}),
           f"MCP {name} takes no caller-chosen execution identity")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nSIMPLIFY-10 task execution: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
