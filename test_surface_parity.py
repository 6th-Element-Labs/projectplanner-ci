#!/usr/bin/env python3
"""QA-8 parity proof for UI-facing REST, MCP, board truth, monitors, and Tally."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from scripts.frontend_test_source import read_frontend_source

_TMP = tempfile.mkdtemp(prefix="surface-parity-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
    import mcp_server  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  surface parity proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "qa8parity"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def mcp_json(payload):
    return json.loads(payload)


try:
    store.create_project(
        "QA8 Parity",
        project_id=P,
        github_repo="6th-Element-Labs/projectplanner",
        actor="seed",
    )
    store.init_db(P)
    dep = store.create_task({"workstream_id": "QA", "title": "parity dependency"},
                            actor="seed", project=P)
    store.update_task(dep["task_id"], {"status": "In Review"}, actor="seed", project=P)
    merged = store.mark_task_merged(
        dep["task_id"],
        "a" * 40,
        pr_number=88,
        pr_url="https://github.com/6th-Element-Labs/projectplanner/pull/88",
        branch="codex/QA-8-parity-fixture",
        head_sha="b" * 40,
        actor="github-webhook",
        project=P,
    )

    task = store.create_task({
        "workstream_id": "QA",
        "title": "surface parity fixture",
        "depends_on": dep["task_id"],
    }, actor="seed", project=P)
    ci_run = store.create_external_ci_run(
        {
            "source_project": P,
            "source_sha": "c" * 40,
            "mirror_repo": "6th-Element-Labs/projectplanner-ci",
            "workflow": "strict.yml",
            "task_id": task["task_id"],
        },
        actor="seed",
        project=P,
    )
    store.update_external_ci_run(
        ci_run["run_id"],
        {
            "status": "success",
            "conclusion": "success",
            "run_url": "https://github.com/6th-Element-Labs/projectplanner-ci/actions/runs/88",
        },
        actor="seed",
        project=P,
    )
    store.set_task_summary(
        task["task_id"],
        f"Blocked on dependencies including {dep['task_id']}.",
        activity_cursor=1,
        project=P,
    )
    store.register_agent("codex/QA8-fixture", "codex", lane="QA",
                         task_id=task["task_id"], ttl_s=600, project=P)
    claim = store.claim_task(task["task_id"], "codex/QA8-fixture",
                             idem_key="qa8-parity-claim", project=P)

    outcome = store.record_outcome("qa", "visible parity proof",
                                   task_id=task["task_id"], project=P)
    verified = store.verify_outcome(outcome["id"], verifier="qa8",
                                    verification="surface parity", project=P)
    kpi = store.create_kpi("visible parity", "proof", "increase", project=P)
    store.link_outcome_to_kpi(verified["id"], kpi["id"], contribution=1,
                              confidence="measured", project=P)
    spend = store.report_usage("agent_report", "reported", task_id=task["task_id"],
                               prompt_tokens=200, completion_tokens=50,
                               cost_usd=2.5, project=P)
    msg = store.send_agent_message(
        "codex/QA8-fixture",
        "codex/QA8-target",
        "ack this parity proof",
        task_id=task["task_id"],
        requires_ack=True,
        ack_timeout_seconds=120,
        project=P,
    )

    identity_task = store.create_task({"workstream_id": "QA", "title": "identity fixture"},
                                      actor="seed", project=P)
    store.add_comment(identity_task["task_id"], "env-mcp-token",
                      "shared-token write without a bound runtime", project=P)

    client = TestClient(app)
    rest_task = client.get(f"/api/tasks/{task['task_id']}", params={"project": P}).json()
    mcp_task = mcp_json(mcp_server.get_task(task["task_id"], project=P))

    ok(rest_task["status"] == mcp_task["status"] == "In Progress",
       "REST and MCP task status match")
    ok(rest_task["depends_on"] == mcp_task["depends_on"] == [dep["task_id"]],
       "REST and MCP dependencies match")
    ok(rest_task["dependency_state"] == mcp_task["dependency_state"],
       "REST and MCP dependency_state match")
    ok(rest_task["rationale_state"] == mcp_task["rationale_state"] and
       rest_task["rationale"] is None and mcp_task["rationale"] is None,
       "stale generated rationale is suppressed consistently")
    ok(rest_task["active_claims"][0]["claim_id"] == claim["claim_id"] and
       mcp_task["active_claims"][0]["claim_id"] == claim["claim_id"],
       "active claims match REST and MCP task detail")
    ok(rest_task["identity"]["status"] == mcp_task["identity"]["status"] == "clear",
       "identity state matches REST and MCP task detail")
    ok(rest_task["external_ci"]["status"] == mcp_task["external_ci"]["status"] == "passed",
       "external CI evidence matches REST and MCP task detail")

    rest_identity = client.get(
        f"/api/tasks/{identity_task['task_id']}", params={"project": P}).json()
    mcp_identity = mcp_json(mcp_server.get_task(identity_task["task_id"], project=P))
    ok(rest_identity["identity"]["takeover_safe"] is False and
       mcp_identity["identity"]["takeover_safe"] is False,
       "identity takeover risk is visible in REST and MCP")

    board = client.get("/api/board", params={"project": P}).json()
    board_dep = next(t for w in board["workstreams"] for t in w["tasks"]
                     if t["task_id"] == dep["task_id"])
    rest_dep = client.get(f"/api/tasks/{dep['task_id']}", params={"project": P}).json()
    ok(board_dep["provenance"]["type"] == rest_dep["provenance"]["type"] == "github_pr_merged",
       "board payload and task detail expose the same Done provenance")
    ok(rest_dep["git_state"]["merged_sha"] == merged["git_state"]["merged_sha"],
       "REST task detail exposes GitHub merge SHA")

    rest_monitors = client.get(
        "/ixp/v1/monitors",
        params={"project": P, "task_id": task["task_id"]},
    ).json()["monitors"]
    mcp_monitors = mcp_json(mcp_server.list_monitors(project=P, task_id=task["task_id"]))
    ok([m["id"] for m in rest_monitors] == [m["id"] for m in mcp_monitors] == [msg["monitor_id"]],
       "REST and MCP task-scoped monitor lists match")

    rest_tally = client.get(f"/tally/v1/task/{task['task_id']}", params={"project": P}).json()
    mcp_tally = mcp_json(mcp_server.get_task_tally(task["task_id"], project=P))
    ok(rest_tally["spend"]["cost_usd"] == mcp_tally["spend"]["cost_usd"] == spend["cost_usd"],
       "REST and MCP task Tally spend match")
    ok(rest_tally["outcomes"]["verified"] == mcp_tally["outcomes"]["verified"] == 1,
       "REST and MCP verified outcome counts match")
    ok(rest_tally["kpis"][0]["verified_contribution"] ==
       mcp_tally["kpis"][0]["verified_contribution"] == 1.0,
       "REST and MCP KPI contribution match")

    app_js = read_frontend_source(Path.cwd())
    ok("controlTruthHtml(t)" in app_js and "dependency_state" in app_js
       and "rationale_state" in app_js and "identity" in app_js
       and "terminal_state" in app_js and "externalCiDetail(t)" in app_js,
       "UI task modal renders structured board truth")
    ok("_loadTaskMonitors(taskId)" in app_js and "/ixp/v1/monitors?" in app_js
       and "task_id=${encodeURIComponent(taskId)}" in app_js,
       "UI task modal loads task-scoped monitors")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
