#!/usr/bin/env python3
"""UI-2: KPI & outcomes operator UI — REST contract + app.js wiring.

Proves the full acceptance flow through the endpoints the SPA calls: define a
KPI, record an outcome, verify it, link it to a KPI, and see KPI movement — plus
the new list endpoints (GET /tally/v1/kpis, /tally/v1/outcomes) the tiles/queue
read, and that static/app.js is wired to all of them."""
import os
import re
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ui2-kpi-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402

P = "switchboard"
passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  UI-2 proof requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

client = TestClient(app)

try:
    store.init_db(P)
    task = store.create_task({"workstream_id": "UI", "title": "move the needle"}, project=P)
    tid = task["task_id"]

    # 1. Define a KPI (create modal → POST /tally/v1/kpis).
    r = client.post("/tally/v1/kpis", json={"project": P, "name": "Weekly active operators",
                                            "unit": "operators", "direction": "increase",
                                            "baseline_value": 2, "current_value": 2, "target_value": 10})
    ok(r.status_code == 200 and not r.json().get("error"), "create KPI returns ok")
    kpi_id = r.json()["id"]

    # 2. KPI list endpoint (tiles) exposes the KPI with a rollup shape.
    r = client.get("/tally/v1/kpis", params={"project": P})
    kpis = r.json().get("kpis", [])
    ok(r.status_code == 200 and any(k["id"] == kpi_id for k in kpis), "GET /tally/v1/kpis lists the KPI")
    tile = next(k for k in kpis if k["id"] == kpi_id)
    ok("verified_contribution" in tile and "spend" in tile, "KPI tile carries rollup fields")

    # 3. Record an outcome against the task (record modal → POST /tally/v1/outcomes).
    r = client.post("/tally/v1/outcomes", json={"project": P, "title": "Shipped operator login",
                                                "type": "feature", "task_id": tid, "status": "proposed"})
    ok(r.status_code == 200 and not r.json().get("error"), "record outcome returns ok")
    outcome_id = r.json()["id"]

    # 4. Verify queue lists the proposed outcome.
    r = client.get("/tally/v1/outcomes", params={"project": P, "status": "proposed"})
    outs = r.json().get("outcomes", [])
    ok(r.status_code == 200 and any(o["id"] == outcome_id for o in outs), "proposed outcome shows in verify queue")
    ok(all("kpi_links" in o for o in outs), "outcomes carry kpi_links for the queue badges")

    # 5. Verify the outcome (Verify button → POST .../verify).
    r = client.post(f"/tally/v1/outcomes/{outcome_id}/verify", json={"project": P})
    ok(r.status_code == 200 and r.json().get("status") == "verified", "verify outcome flips status to verified")

    # 6. Link the verified outcome to the KPI (link modal → POST /tally/v1/outcome_kpi_links).
    r = client.post("/tally/v1/outcome_kpi_links", json={"project": P, "outcome_id": outcome_id,
                                                         "kpi_id": kpi_id, "contribution": 3,
                                                         "confidence": "measured"})
    ok(r.status_code == 200 and not r.json().get("error"), "link outcome to KPI returns ok")

    # 7. See KPI movement — the verified, linked contribution rolls up.
    r = client.get("/tally/v1/kpis", params={"project": P})
    tile = next(k for k in r.json()["kpis"] if k["id"] == kpi_id)
    ok(abs(float(tile["verified_contribution"]) - 3.0) < 1e-9, "verified contribution rolls up onto the KPI tile")

    # 8. Reject path is reachable for a second outcome.
    r2 = client.post("/tally/v1/outcomes", json={"project": P, "title": "Flaky attempt", "type": "fix"})
    oid2 = r2.json()["id"]
    r = client.post(f"/tally/v1/outcomes/{oid2}/reject", json={"project": P, "reason": "did not land"})
    ok(r.status_code == 200 and r.json().get("status") == "rejected", "reject outcome flips status to rejected")

    # 9. Update KPI value (Update value → PATCH /tally/v1/kpis/{id}).
    r = client.patch(f"/tally/v1/kpis/{kpi_id}", json={"project": P, "current_value": 5})
    ok(r.status_code == 200 and float(r.json().get("current_value")) == 5.0, "update KPI current value")

    # 10. app.js is wired to the whole surface.
    here = os.path.dirname(os.path.abspath(__file__))
    appjs = open(os.path.join(here, "static", "app.js")).read()
    needles = ["_missionKpiOutcomesHtml", "loadKpisAndOutcomes", "tally/v1/kpis",
               "tally/v1/outcomes", "tally/v1/outcome_kpi_links", "submitKpi",
               "verifyTallyOutcome", "rejectTallyOutcome", "submitKpiLink",
               'data-dl-action="kpi-new"', 'data-dl-action="outcome-verify"']
    missing = [n for n in needles if n not in appjs]
    ok(not missing, f"app.js wired to KPI/outcome UI (missing: {missing})")
    indexhtml = open(os.path.join(here, "static", "index.html")).read()
    modals = ["dl-kpi-modal", "dl-tally-outcome-modal", "dl-kpi-link-modal"]
    ok(all(m in indexhtml for m in modals), "index.html has the KPI/outcome modal shells")
except Exception:
    import traceback
    traceback.print_exc()
    failed += 1
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
