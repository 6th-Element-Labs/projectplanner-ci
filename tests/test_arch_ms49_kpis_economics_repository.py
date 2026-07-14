#!/usr/bin/env python3
"""ARCH-MS-49: kpis/outcomes/spend under storage/repositories/kpis_economics."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms49-kpis-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


for name in (
    "switchboard.storage.repositories.kpis_economics",
    "kpis_economics_store",
):
    try:
        importlib.import_module(name)
        ok(True, f"{name} imports cleanly")
    except Exception as exc:  # noqa: BLE001
        ok(False, f"{name} import failed: {exc!r}")

ok((ROOT / "src/switchboard/storage/repositories/kpis_economics.py").is_file(),
   "kpis_economics.py exists under storage/repositories")
ok((ROOT / "kpis_economics_store.py").is_file(),
   "kpis_economics_store.py shim exists at repo root")

from switchboard.storage.repositories import kpis_economics as kpi_repo  # noqa: E402
import kpis_economics_store  # noqa: E402
import store  # noqa: E402

ok(kpis_economics_store.report_usage is kpi_repo.report_usage,
   "kpis_economics_store shim re-exports report_usage")
ok(kpis_economics_store.create_kpi is kpi_repo.create_kpi,
   "kpis_economics_store shim re-exports create_kpi")
ok(store.report_usage is kpi_repo.report_usage,
   "store facade delegates report_usage to package module")
ok(store.record_outcome is kpi_repo.record_outcome,
   "store facade delegates record_outcome to package module")
ok(store.verify_outcome is kpi_repo.verify_outcome,
   "store facade delegates verify_outcome to package module")
ok(store.reject_outcome is kpi_repo.reject_outcome,
   "store facade delegates reject_outcome to package module")
ok(store.create_kpi is kpi_repo.create_kpi,
   "store facade delegates create_kpi to package module")
ok(store.update_kpi_value is kpi_repo.update_kpi_value,
   "store facade delegates update_kpi_value to package module")
ok(store.link_outcome_to_kpi is kpi_repo.link_outcome_to_kpi,
   "store facade delegates link_outcome_to_kpi to package module")
ok(store.kpi_tally is kpi_repo.kpi_tally,
   "store facade delegates kpi_tally to package module")
ok(store.list_kpis is kpi_repo.list_kpis,
   "store facade delegates list_kpis to package module")
ok(store.list_outcomes is kpi_repo.list_outcomes,
   "store facade delegates list_outcomes to package module")
ok(store._merge_spend_totals is kpi_repo._merge_spend_totals,
   "store facade delegates _merge_spend_totals to package module")
ok(store._dispatch_score is kpi_repo._dispatch_score,
   "store facade delegates _dispatch_score to package module")
ok(store.report_usage.__module__
   == "switchboard.storage.repositories.kpis_economics",
   "report_usage lives under switchboard.storage.repositories.kpis_economics")
ok(store.create_kpi.__module__
   == "switchboard.storage.repositories.kpis_economics",
   "create_kpi lives under switchboard.storage.repositories.kpis_economics")
ok(isinstance(store.kpis_economics_repository, kpi_repo.StoreKpisEconomicsRepository),
   "store.kpis_economics_repository is StoreKpisEconomicsRepository")

shell_src = (ROOT / "src/switchboard/storage/repositories/shell.py").read_text()
kpi_src = (ROOT / "src/switchboard/storage/repositories/kpis_economics.py").read_text()
ok("def report_usage(" not in shell_src,
   "shell residual no longer defines report_usage")
ok("def create_kpi(" not in shell_src,
   "shell residual no longer defines create_kpi")
ok("def kpi_tally(" not in shell_src,
   "shell residual no longer defines kpi_tally")
ok("def list_outcomes(" not in shell_src,
   "shell residual no longer defines list_outcomes")
ok("def _dispatch_score(" not in shell_src,
   "shell residual no longer defines _dispatch_score")
ok("def _risk_value(" not in shell_src,
   "_risk_value no longer defined in shell residual (ARCH-MS-50 → claims)")
ok(store._risk_value.__module__
   == "switchboard.storage.repositories.claims",
   "_risk_value now lives under claims after ARCH-MS-50")
ok("def report_usage(" in kpi_src,
   "kpis_economics repository owns report_usage")
ok("def create_kpi(" in kpi_src,
   "kpis_economics repository owns create_kpi")
ok(len(kpi_src.splitlines()) > 400,
   "kpis_economics extract is substantial")
ok(len(shell_src.splitlines()) < 4000,
   "shell residual shrunk after ARCH-MS-49 extract")

try:
    store.init_project_registry()
    store.init_db("switchboard")
    created_task = store.create_task(
        {"workstream_id": "ARCH-MS", "title": "ms49 kpis economics proof",
         "description": "kpis/outcomes/spend repository extract"},
        actor="arch-ms49",
        project="switchboard",
    )
    ok(bool(created_task and created_task.get("task_id")),
       "create_task persists a task for KPI proof")
    task_id = created_task["task_id"]

    spend = store.report_usage(
        source="agent",
        confidence="measured",
        task_id=task_id,
        agent_id="arch-ms49",
        model="test-model",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.01,
        project="switchboard",
    )
    ok(isinstance(spend, dict) and "id" in spend,
       f"report_usage via store façade ({spend.get('error')})")

    outcome = store.record_outcome(
        outcome_type="ship",
        title="ms49 outcome",
        task_id=task_id,
        actor="arch-ms49",
        project="switchboard",
    )
    ok(isinstance(outcome, dict) and outcome.get("id"),
       f"record_outcome via store façade ({outcome.get('error')})")

    kpi = store.create_kpi(
        name="ms49-kpi",
        unit="ships",
        direction="increase",
        baseline_value=0,
        current_value=0,
        target_value=10,
        actor="arch-ms49",
        project="switchboard",
    )
    ok(isinstance(kpi, dict) and kpi.get("id"),
       f"create_kpi via store façade ({kpi.get('error')})")

    link = store.link_outcome_to_kpi(
        outcome["id"], kpi["id"], contribution=1.0, actor="arch-ms49",
        project="switchboard",
    )
    ok(isinstance(link, dict) and link.get("id"),
       f"link_outcome_to_kpi via store façade ({link.get('error')})")

    verified = store.verify_outcome(
        outcome["id"], verifier="arch-ms49", project="switchboard",
    )
    ok(verified.get("status") == "verified",
       f"verify_outcome via store façade ({verified.get('error')})")

    tally = store.kpi_tally(kpi["id"], project="switchboard")
    ok(isinstance(tally, dict) and "spend" in tally and "outcomes" in tally,
       "kpi_tally reachable via store façade")

    kpis = store.list_kpis(project="switchboard")
    outcomes = store.list_outcomes(project="switchboard")
    ok(isinstance(kpis, list) and any(k.get("id") == kpi["id"] for k in kpis),
       "list_kpis includes created KPI")
    ok(isinstance(outcomes, list) and any(o.get("id") == outcome["id"] for o in outcomes),
       "list_outcomes includes created outcome")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nARCH-MS-49 kpis_economics repository: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
