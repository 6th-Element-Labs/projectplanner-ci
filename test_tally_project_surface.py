#!/usr/bin/env python3
"""Self-contained smoke for the TALLY-3 project-level board surface endpoint."""
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tally-project-")
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
except ModuleNotFoundError as exc:
    print(f"  SKIP  FastAPI endpoint smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)


P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    store.init_db(P)
    task = store.create_task({"workstream_id": "TALLY", "title": "economic surface"}, project=P)
    spend = store.report_usage(source="agent_report", confidence="reported",
                               task_id=task["task_id"], cost_usd=1.25,
                               prompt_tokens=100, completion_tokens=25, project=P)
    outcome = store.record_outcome("feature", "visible cost per outcome",
                                   task_id=task["task_id"], project=P)
    verified = store.verify_outcome(outcome["id"], verifier="test",
                                    verification="endpoint", project=P)
    kpi = store.create_kpi("verified outcomes", "outcome", "increase", project=P)
    store.link_outcome_to_kpi(verified["id"], kpi["id"], contribution=1,
                              confidence="measured", project=P)

    client = TestClient(app)
    res = client.get("/tally/v1/project", params={"project": P})
    ok(res.status_code == 200, "project Tally endpoint returns 200")
    data = res.json()
    ok(data["totals"]["spend"]["cost_usd"] == spend["cost_usd"],
       "project Tally endpoint exposes total spend")
    ok(data["totals"]["unit_cost"]["cost_per_verified_outcome"] == 1.25,
       "project Tally endpoint exposes cost per verified outcome")
    ok(any(t["task_id"] == task["task_id"] for t in data["by_task"]),
       "project Tally endpoint includes task economics")
    ok(any(k["kpi"]["id"] == kpi["id"] for k in data["kpis"]),
       "project Tally endpoint includes KPI economics")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
