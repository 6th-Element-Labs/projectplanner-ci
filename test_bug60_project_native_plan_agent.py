#!/usr/bin/env python3
"""Regression proof for BUG-60: project-native tools and queued Ask Taikun."""

import asyncio
import json
import os
import tempfile
import time
import types

_TMP = tempfile.mkdtemp(prefix="bug60-plan-agent-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"

import agent  # noqa: E402
import background_jobs  # noqa: E402
import project_contract  # noqa: E402
import store  # noqa: E402
from switchboard.api.routers.plan_chat import create_router  # noqa: E402


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def tool(tools, name):
    return next(item["function"] for item in tools if item["function"]["name"] == name)


PROJECT = "zscaler-qa"
store.init_project_registry()
store.create_project(
    "Zscaler QA",
    project_id=PROJECT,
    purpose="Validate a Zscaler proof of concept",
    boundary="Only Zscaler QA delivery work belongs here.",
    actor="test",
)
store.create_task(
    {
        "workstream_id": "ACCESS",
        "title": "Validate access policy",
        "owner_org": "Zscaler",
        "phase": "Discovery",
    },
    actor="test",
    project=PROJECT,
)
store.create_task(
    {
        "workstream_id": "PMO",
        "title": "Run the project cadence",
        "owner_org": "Taikun",
        "phase": "Delivery",
    },
    actor="test",
    project=PROJECT,
)

tools = agent.tools_for_project(PROJECT)
doc_tool = tool(tools, "doc_search")
new_task_tool = tool(tools, "propose_new_task")
update_tool = tool(tools, "propose_task_update")
ok(
    "Zscaler QA" in doc_tool["description"] and "TEEP" not in doc_tool["description"],
    "doc_search is scoped to the selected project's corpus",
)
ok(
    tool(tools, "get_project_contract")["name"] == "get_project_contract",
    "the embedded agent exposes the same project-contract capability as MCP",
)
ok(
    new_task_tool["parameters"]["properties"]["workstream_id"]["enum"]
    == ["ACCESS", "PMO"],
    "new-task workstreams come from the live selected board",
)
ok(
    set(update_tool["parameters"]["properties"]["owner_org"]["enum"])
    == {"Taikun", "Zscaler"},
    "owner organizations come from the selected project",
)
ok(
    set(update_tool["parameters"]["properties"]["phase"]["enum"])
    == {"Delivery", "Discovery"},
    "phases come from the selected project",
)

voice = agent._project_voice(PROJECT)
triage_prompt = agent._system_triage(project=PROJECT)
ok(
    "Zscaler QA" in voice["who"] and "Maxwell" not in voice["who"],
    "generic projects get their own agent identity",
)
ok(
    "ACCESS-1" in triage_prompt
    and "Maxwell" not in triage_prompt
    and "TEEP Barnett" not in triage_prompt,
    "triage reads the selected board without Maxwell leakage",
)
contract = project_contract.build(PROJECT)
ok(
    contract["project"] == PROJECT
    and contract["project_label"] == "Zscaler QA"
    and contract["project_access"]["purpose"] == "Validate a Zscaler proof of concept",
    "MCP and the embedded agent share a project-native contract service",
)

original_run = agent.run


def fake_run(_task, question, history=None, project="maxwell", **_kwargs):
    return {
        "answer": f"plan for {project}: {question}",
        "proposal": None,
        "proposals": [],
        "new_tasks": [{"workstream_id": "PMO", "title": "Confirm scope"}],
        "sources": ["zscaler-kickoff.docx"],
        "recipients": None,
        "dispatch_targets": [],
    }


agent.run = fake_run
try:
    started = time.perf_counter()
    queued = background_jobs.enqueue_background_job(
        PROJECT,
        "plan_agent_run",
        params={
            "question": "build the overall project plan",
            "history": [],
            "session": "plan",
            "record_chat": True,
        },
        actor="test",
    )
    elapsed = time.perf_counter() - started
    ok(
        queued["status"] == "pending" and elapsed < 0.5,
        "enqueue returns pending without waiting for the agent",
    )
    completed = queued
    for _ in range(100):
        completed = background_jobs.load_run(PROJECT, queued["run_id"])
        if completed.get("status") in background_jobs.TERMINAL_RUN_STATUSES:
            break
        time.sleep(0.02)
    result = completed["steps"][0].get("result") or {}
    ok(
        completed["status"] == "completed"
        and result.get("answer", "").startswith(f"plan for {PROJECT}"),
        "queued agent result is persisted and pollable",
    )
    chats = store.recent_chat("plan", 20, project=PROJECT)
    ok(
        any(
            item["role"] == "assistant"
            and item["payload"].get("run_id") == queued["run_id"]
            for item in chats
        ),
        "completed runs persist their assistant response exactly once",
    )

    paused = background_jobs.enqueue_background_job(
        PROJECT,
        "plan_agent_run",
        params={"question": "resume me", "history": [], "record_chat": False},
        actor="test",
        start_worker=False,
    )
    background_jobs.ensure_background_job_running(
        PROJECT, paused["run_id"], actor="test/resume"
    )
    resumed = paused
    for _ in range(100):
        resumed = background_jobs.load_run(PROJECT, paused["run_id"])
        if resumed.get("status") in background_jobs.TERMINAL_RUN_STATUSES:
            break
        time.sleep(0.02)
    ok(
        resumed["status"] == "completed",
        "a persisted pending run resumes when the UI or MCP client reconnects",
    )

    def failing_run(*_args, **_kwargs):
        raise RuntimeError("gateway unavailable")

    agent.run = failing_run
    failed_run = background_jobs.enqueue_background_job(
        PROJECT,
        "plan_agent_run",
        params={
            "question": "fail visibly",
            "history": [],
            "session": "failure",
            "record_chat": True,
        },
        actor="test",
    )
    failed_manifest = failed_run
    for _ in range(100):
        failed_manifest = background_jobs.load_run(PROJECT, failed_run["run_id"])
        if failed_manifest.get("status") in background_jobs.TERMINAL_RUN_STATUSES:
            break
        time.sleep(0.02)
    failure_chat = store.recent_chat("failure", 20, project=PROJECT)
    ok(
        failed_manifest["status"] == "failed"
        and any(
            item["payload"].get("run_id") == failed_run["run_id"]
            and item["payload"].get("error") == "gateway unavailable"
            for item in failure_chat
        ),
        "failed runs persist a visible reconnect-safe chat error",
    )
    agent.run = fake_run

    router = create_router(resolve_project=lambda value: value)
    post = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/chat" and "POST" in route.methods
    )
    request = types.SimpleNamespace(state=types.SimpleNamespace(principal=None))
    response = asyncio.run(
        post(
            request,
            body={"message": "make the Maxwell-style project plan", "session": "plan"},
            project=PROJECT,
        )
    )
    accepted = json.loads(response.body)
    ok(
        response.status_code == 202
        and accepted["status"] == "pending"
        and accepted["run_id"].startswith("bgjob-plan_agent_run-"),
        "REST Ask Taikun returns HTTP 202 with a pollable run id",
    )
finally:
    agent.run = original_run

ui_source = (
    open("static/app.js", encoding="utf-8").read()
    + open("static/js/plan-chat.js", encoding="utf-8").read()
)
html_source = open("static/index.html", encoding="utf-8").read()
ok(
    "_pollAskRun" in ui_source and "api/chat/runs/latest" in ui_source,
    "Ask Taikun polls and resumes queued runs",
)
ok(
    'id="ask-build-plan"' in html_source and "buildProjectPlan()" in ui_source,
    "the UI exposes the reusable Build project plan action",
)
ok(
    "Maxwell is reading the plan" not in ui_source,
    "generic Ask Taikun progress copy no longer says Maxwell",
)
ok(
    "this._renderAskResult(run)" not in ui_source
    and "never resurrect a run removed by Clear" in ui_source,
    "reconnect refreshes durable chat instead of resurrecting cleared runs",
)
ok(
    "m.payload && m.payload.error" in ui_source,
    "durable failed-run chat entries render as errors",
)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
