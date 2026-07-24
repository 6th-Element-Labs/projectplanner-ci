#!/usr/bin/env python3
"""DISPATCH-12: every task start is a minimal Connect assignment."""

from __future__ import annotations

import ast
import os
from pathlib import Path

from path_setup import ROOT

from switchboard.application.commands import connect_dispatch
from switchboard.application.commands import task_execution
from switchboard.connect.execution_assignment import build_execution_assignment
from execution_policy_fixture import ready_execution_context


passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


captured: list[dict] = []
original_request_wake = connect_dispatch.coordination_repo.request_wake
original_resolve = connect_dispatch.execution_context.resolve


def fake_request_wake(**kwargs):
    captured.append(kwargs)
    return {"wake_id": f"wake-{len(captured)}", "status": "pending"}


connect_dispatch.coordination_repo.request_wake = fake_request_wake
connect_dispatch.execution_context.resolve = lambda **kwargs: ready_execution_context(
    kwargs["task_id"], runtime=kwargs["runtime"])
try:
    for runtime in ("codex", "claude", "cursor"):
        result = connect_dispatch.enqueue_task(
            {"task_id": "DISPATCH-12", "_wsId": "DISPATCH",
             "updated_at": 1784662000.0},
            project="switchboard", actor="dispatch12-test", runtime=runtime,
        )
        ok(result.get("dispatched") is True,
           f"{runtime} enters the same Connect dispatcher")
    connect_dispatch.enqueue_task(
        {"task_id": "DISPATCH-12", "_wsId": "DISPATCH",
         "updated_at": 1784662000.0},
        project="switchboard", actor="another-start-surface", runtime="codex",
    )
finally:
    connect_dispatch.coordination_repo.request_wake = original_request_wake
    connect_dispatch.execution_context.resolve = original_resolve

request_payload_fields = ("selector", "reason", "source", "policy", "task_id")
ok(captured[0]["idem_key"] == captured[-1]["idem_key"]
   and all(captured[0][field] == captured[-1][field]
           for field in request_payload_fields),
   "simultaneous Starts from different surfaces reuse one idempotent payload")
captured.pop()

for row in captured:
    policy = row["policy"]
    assignment = policy.get("assignment") or {}
    lifecycle = policy.get("lifecycle") or {}
    ok({"mode", "assignment", "lifecycle", "scheduler", "placement",
        "execution_context"}.issubset(policy)
       and policy["mode"] == "connect"
       and policy["scheduler"]["mode"] == "hybrid",
       "durable wake policy keeps lifecycle identity and hybrid placement")
    ok(set(assignment) == {
        "schema", "assignment_id", "principal_ref", "work_ref", "runtime",
        "provider", "workspace_ref", "limits", "queued_at",
    } and assignment["schema"] == "switchboard.connect.assignment.v1",
       "Assignment v1 remains byte-compatible")
    ok(set(lifecycle) == {
        "schema", "role", "head_sha", "pr_number", "pr_url", "ttl_seconds",
    }, "sibling lifecycle request leaves identity allocation to the server")
    forbidden = {
        "mcp", "token", "credential", "claim", "work_session", "review",
        "evidence", "instruction", "prompt", "pull_request",
        "done", "lifecycle", "completion",
    }
    words = {str(key).lower() for key in assignment}
    ok(not words.intersection(forbidden),
       "Connect assignment contains no communication or orchestration fields")

app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
runner_ui = (ROOT / "static" / "js" / "runner-session.js").read_text(encoding="utf-8")
dispatch_block = app[app.index("async dispatchTask"):app.index("async _openDirectRunnerWhenReady")]
ok("/start`" in dispatch_block and "/dispatch`" not in dispatch_block
   and "runtime: rt" in dispatch_block,
   "browser provider buttons all call the unified Start operation")
ok("if (data.action === 'attach' || data.action === 'starting')" in dispatch_block
   and "rt === 'codex' && (data.action" not in dispatch_block,
   "attach and in-flight dedupe behave identically for every provider")
resume_block = runner_ui[
    runner_ui.index("async resumeTaskReview"):
    runner_ui.index("async _runnerPtyConnect")
]
ok("/start`" in resume_block and "/resume-review" not in resume_block,
   "review resume is just Start/attach, not a lifecycle dispatcher")

task_execution_source = (ROOT / "src" / "switchboard" / "application" / "commands"
                         / "task_execution.py").read_text(encoding="utf-8")
ok("connect_dispatch.enqueue_task" in task_execution_source
   and "import dispatch as dispatch_mod" not in task_execution_source,
   "Task Execution reaches Connect directly and bypasses legacy dispatch.py")

saved_projection = task_execution._projection
saved_ticket = task_execution.runner_pty_command.mint_ticket_for_session
saved_enqueue = connect_dispatch.enqueue_task
unexpected_launches: list[dict] = []
try:
    task_execution.runner_pty_command.mint_ticket_for_session = lambda **_kwargs: {}
    connect_dispatch.enqueue_task = (
        lambda *args, **kwargs: unexpected_launches.append(kwargs) or {
            "dispatched": True, "wake_id": "wake-unexpected"})
    task_execution._projection = lambda *_args, **_kwargs: {
        "task": {"task_id": "DISPATCH-12"},
        "active_runner": {
            "runner_session_id": "run-existing", "host_id": "host/existing"},
    }
    attached = task_execution.start_task("DISPATCH-12", project="switchboard")
    ok(attached.get("action") == "attach"
       and attached.get("execution_id") == "run-existing"
       and not unexpected_launches,
       "Start attaches to a live agent instead of creating a duplicate assignment")

    task_execution._projection = lambda *_args, **_kwargs: {
        "task": {"task_id": "DISPATCH-12"},
        "active_attempt": {"wake_id": "wake-existing", "status": "pending"},
    }
    starting = task_execution.start_task("DISPATCH-12", project="switchboard")
    ok(starting.get("action") == "starting"
       and starting.get("wake_id") == "wake-existing"
       and not unexpected_launches,
       "Start dedupes an in-flight assignment instead of creating another wake")
finally:
    task_execution._projection = saved_projection
    task_execution.runner_pty_command.mint_ticket_for_session = saved_ticket
    connect_dispatch.enqueue_task = saved_enqueue

saved_projection = task_execution._projection
saved_cancel = task_execution.coordination_repo.cancel_wake
retry_launches: list[dict] = []
try:
    retry_projections = iter((
        {"task": {"task_id": "DISPATCH-12"},
         "active_attempt": {"wake_id": "wake-claude", "status": "pending",
                            "runtime": "claude-code"}},
        {"task": {"task_id": "DISPATCH-12"}},
    ))
    task_execution._projection = lambda *_args, **_kwargs: next(retry_projections)
    task_execution.coordination_repo.cancel_wake = (
        lambda *_args, **_kwargs: {"cancelled": True})
    task_execution.retry_task(
        "DISPATCH-12", project="switchboard",
        launcher=lambda *_args, **kwargs: retry_launches.append(kwargs) or {
            "action": "started", "started": True, "wake_id": "wake-retry"},
    )
finally:
    task_execution._projection = saved_projection
    task_execution.coordination_repo.cancel_wake = saved_cancel
ok(bool(retry_launches) and retry_launches[0].get("runtime") == "claude-code",
   "Retry preserves the provider runtime instead of silently switching to Codex")

legacy_calls = []
for path in ROOT.rglob("*.py"):
    rel = path.relative_to(ROOT).as_posix()
    if (rel == "dispatch.py" or rel.startswith(("tests/", "scripts/"))
            or path.name.startswith("test_")):
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (isinstance(owner, ast.Name) and owner.id in {"dispatch", "dispatch_mod"}
                and node.func.attr in {
                    "dispatch", "start_task", "resume_review", "dispatch_to_co_fleet",
                }):
            legacy_calls.append(f"{rel}:{node.lineno}")
ok(not legacy_calls, f"no product start surface calls a legacy launcher ({legacy_calls})")

# Host translation consumes the same contract and uses host-local provider syntax.
import adapters.agent_host as agent_host  # noqa: E402

host_source = (ROOT / "adapters" / "agent_host.py").read_text(encoding="utf-8")
ok('if wake_mode(w, inventory) == "connect":' in host_source
   and "runner_session_id = _runner_session_id_for_wake(w, host_id)" in host_source,
   "Connect binds one runner identity across claim, Ack, supervisor, and registry")
ok('if wake_mode(claimed_wake, inventory) == "connect":' in host_source
   and "launch_env = {}" in host_source,
   "Connect launch bypasses legacy claim and Work Session bootstrap environment")

os.environ["PM_RUNNER_LEASE_ENFORCEMENT"] = "1"
saved_work_module = os.environ.pop("PM_AGENT_WORK_MODULE", None)
for row in captured:
    runtime = row["selector"]["runtime"]
    host_policy = {
        **row["policy"],
        "lifecycle": {
            **row["policy"]["lifecycle"],
            "execution_id": f"execlease-dispatch12-{runtime}",
            "generation": 1,
            "fence_epoch": 1,
        },
    }
    host_policy["execution_assignment"] = build_execution_assignment(
        task_id="DISPATCH-12",
        assignment=host_policy["assignment"],
        lifecycle=host_policy["lifecycle"],
    )
    wake = {
        "wake_id": "wake-host-test", "task_id": "DISPATCH-12",
        "selector": row["selector"], "policy": host_policy,
    }
    inventory = {
        "host_id": "host/test", "repo_root": str(ROOT),
        "policy": {"allow_work": True},
        "runtimes": [{
            "runtime": runtime, "provider": row["selector"]["provider"],
            "lanes": ["DISPATCH"], "capabilities": [
                "execution_lease_v2", "runner_lease_enforcement"],
            "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
        }],
    }
    command, mode = agent_host.launch_command(
        wake, inventory, runner_session_id="run-host-test")
    note = next((part for part in reversed(command)
                 if isinstance(part, str) and "via Switchboard" in part), "")
    ok(mode == "connect" and "Do DISPATCH-12 in project" in note
       and "via Switchboard" in note
       and "prepare_agent_session" in note,
       f"{runtime} host launch uses the via-Switchboard MCP boot note")
    if runtime == "codex":
        ok("mcp_servers.taikun_plan.required=true" in command
           and "SWITCHBOARD_CONNECT_SESSION_TOKEN" in " ".join(command),
           "codex Connect requires host taikun_plan MCP at launch")
os.environ.pop("PM_RUNNER_LEASE_ENFORCEMENT", None)
if saved_work_module is not None:
    os.environ["PM_AGENT_WORK_MODULE"] = saved_work_module

codex_row = captured[0]
wrong_provider_inventory = {
    "host_id": "host/wrong-provider", "repo_root": str(ROOT),
    "policy": {"allow_work": True},
    "runtimes": [{
        "runtime": "codex", "provider": "anthropic", "lanes": ["DISPATCH"],
        "capabilities": [],
        "policy": {"allow_work": True, "lane_mode": "all_project_lanes"},
    }],
}
wrong_provider_wake = {
    "wake_id": "wake-provider-mismatch", "task_id": "DISPATCH-12",
    "selector": codex_row["selector"], "policy": codex_row["policy"],
}
ok(agent_host.eligible_runtime(wrong_provider_wake, wrong_provider_inventory) is None,
   "host eligibility requires the exact runtime and provider pair")

saved_require = agent_host._require
registration_calls: list[dict] = []
try:
    agent_host._require = lambda method, path, body=None: (
        registration_calls.append({"method": method, "path": path, "body": body})
        or dict(body or {}))
    agent_host.register_runner_session(
        {"runner_session_id": "run-connect-register", "wake_mode": "connect",
         "runtime": "codex", "status": "running"},
        wrong_provider_wake,
        {"host_id": "host/register", "repo_root": str(ROOT)},
    )
finally:
    agent_host._require = saved_require
registered_metadata = ((registration_calls[0]["body"] if registration_calls else {})
                       .get("metadata") or {})
ok(bool(registration_calls)
   and registered_metadata.get("connect_assignment") is True
   and registered_metadata.get("assignment_id")
   and not ({"role", "lifecycle_role"} & set(registered_metadata)),
   "Connect runner registration is fail-closed and contains no lifecycle role")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
