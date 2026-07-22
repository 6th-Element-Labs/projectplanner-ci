#!/usr/bin/env python3
"""UI-8: prove the Fleet control tab REST contract + UI wiring.

Surfaces the MCP-only fleet substrate (agent hosts, wake queue, runner actions) in the
web UI: a new Fleet tab with a hosts table, a wake-intents queue (wake / cancel), and
runner rows with logs/snapshot and a human-gated kill. Builds on UI-3's health pattern.
Same "API + app.js-needle" shape as test_work_session_health_panel.py.
"""
import os
import shutil
import sys
import tempfile
from scripts.frontend_test_source import read_frontend_source

_TMP = tempfile.mkdtemp(prefix="fleet-control-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  fleet control proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

P = "qa-fleet"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


client = TestClient(app)

try:
    store.init_project_registry()
    store.create_project("Fleet QA", project_id=P, actor="test")
    store.init_db(P)

    store.register_host(
        {"host_id": "host/qa-1", "hostname": "qa-box",
         "runtimes": [{"runtime": "claude-code"}],
         "limits": {"max_sessions": 2}, "capacity": {"active_sessions": 1}},
        actor="test", project=P)

    # ---- Hosts table --------------------------------------------------------
    hosts = client.get("/ixp/v1/agent_hosts", params={"project": P, "include_stale": True})
    ok(hosts.status_code == 200, "GET /ixp/v1/agent_hosts returns 200")
    hlist = hosts.json().get("hosts") or []
    ok(any(h.get("host_id") == "host/qa-1" for h in hlist),
       "hosts table can list the registered host")
    h0 = next((h for h in hlist if h.get("host_id") == "host/qa-1"), {})
    ok("heartbeat_at" in h0 and (h0.get("limits") or {}).get("max_sessions") == 2,
       "host row carries heartbeat + capacity for the table")

    # ---- Wake queue: request -> list -> cancel ------------------------------
    req = client.post("/ixp/v1/request_wake", json={
        "project": P, "selector": {"runtime": "claude-code", "lane": "UI"},
        "reason": "operator wake from Fleet", "task_id": "UI-8"})
    ok(req.status_code == 200, "POST /ixp/v1/request_wake returns 200")
    wake_id = req.json().get("wake_id")
    ok(bool(wake_id), "request_wake returns a wake_id")

    listed = client.get("/ixp/v1/wake_intents", params={"project": P})
    ok(listed.status_code == 200, "GET /ixp/v1/wake_intents returns 200")
    wl = listed.json().get("wake_intents") or []
    mine = next((w for w in wl if w.get("wake_id") == wake_id), {})
    ok(mine.get("status") in ("pending", "claimed"),
       "wake queue shows the new intent as active")
    ok((mine.get("selector") or {}).get("runtime") == "claude-code" and mine.get("task_id") == "UI-8",
       "wake row carries selector + task for the queue")

    cancelled = client.post("/ixp/v1/cancel_wake", json={"project": P, "wake_id": wake_id})
    ok(cancelled.status_code == 200, "POST /ixp/v1/cancel_wake returns 200")
    after = client.get("/ixp/v1/wake_intents", params={"project": P, "status": "cancelled"}).json()
    ok(any(w.get("wake_id") == wake_id for w in (after.get("wake_intents") or [])),
       "cancel moves the intent to cancelled")

    # ---- Runners ------------------------------------------------------------
    runners = client.get("/ixp/v1/runner_sessions", params={"project": P, "include_stale": True})
    ok(runners.status_code == 200 and isinstance(runners.json().get("sessions"), list),
       "GET /ixp/v1/runner_sessions returns the runner list for the fleet table")

    # ---- index.html shell ----------------------------------------------------
    index = client.get("/")
    for needle in ('id="tab-fleet"', 'id="toptab-fleet"', 'id="wake-modal"', 'id="fleet-hosts-body"'):
        ok(index.status_code == 200 and needle in index.text,
           f"index.html exposes {needle}")

    # ---- app.js wiring -------------------------------------------------------
    app_js = read_frontend_source(os.path.dirname(__file__))
    for needle in (
        "renderFleet",
        "_loadFleetHosts",
        "_hostRow",
        "_loadWakeIntents",
        "_wakeRow",
        "_openWakeModal",
        "_submitWake",
        "_cancelWake",
        "_loadFleetRunners",
        "_fleetRunnerAction",
    ):
        ok(needle in app_js, f"app.js defines {needle}")
    ok("window.prompt" in app_js and "Kill is destructive" in app_js,
       "runner kill is human-gated with a typed confirm")

    # BUG-68: applyProject must not clobber Fleet's .page-title via a document-wide
    # querySelector — only a dedicated #project-header (if present) may be branded.
    ok("document.querySelector('.page-title')" not in app_js
       and 'document.querySelector(".page-title")' not in app_js,
       "app.js does not use a document-wide .page-title querySelector (BUG-68)")
    ok("getElementById('project-header')" in app_js
       or 'getElementById("project-header")' in app_js,
       "applyProject scopes optional branding to #project-header")
    ok('>Fleet</h2>' in index.text and 'id="tab-fleet"' in index.text,
       "Fleet tab keeps its own page-title heading")
    ok(index.text.index('id="fleet-runners-body"') < index.text.index('id="fleet-hosts-body"')
       < index.text.index('id="fleet-wakes-body"'),
       "Fleet puts live runners above hosts and wake intents")
    ok("Runner capacity" in index.text and "Launch queue" in index.text,
       "Fleet uses operator-facing capacity and launch labels")
    for body_id in ("fleet-runners-body", "fleet-hosts-body", "fleet-wakes-body"):
        ok(f'data-bs-target="#{body_id}"' in index.text
           and f'aria-controls="{body_id}"' in index.text
           and f'id="{body_id}" class="card-body collapse show"' in index.text,
           f"Fleet exposes an expanded Tabler collapse control for {body_id}")

finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print(f"\nFleet control proof: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
