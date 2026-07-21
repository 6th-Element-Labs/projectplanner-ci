#!/usr/bin/env python3
"""ARCH-MS-92: Tasks Caddy cutover + dual-strip (Path A Go)."""
from __future__ import annotations

import ast
import importlib
import os
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms92-tasks-cut-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms92"
Path(os.environ["PM_DYNAMIC_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- live edge + unit --------------------------------------------------------
caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
live = "\n".join(
    line for line in caddy.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
ok("handle /api/tasks*" in live or "handle /api/tasks" in live,
   "live Caddy routes /api/tasks*")
ok("8122" in live, "live Caddy points Tasks at :8122")
ok("@tasks_sibling path_regexp tasks_sibling" in live,
   "live Caddy carves dispatch/chat/review siblings with one anchored matcher")
ok(
    "dispatch|chat|resume-review|start|execution|review_" in live,
    "sibling matcher covers dispatch, chat, task execution, and review",
)
ok("handle @tasks_sibling" in live, "sibling matcher routes through its own handle")
ok("/txp/v1/claim_next" in live and "/txp/v1/complete_claim" in live,
   "live Caddy routes claim-only TXP")
ok("handle /txp/v1/*" not in live, "no blanket /txp/v1/* handle")

unit = ROOT / "deploy" / "switchboard-tasks.service"
ok(unit.is_file(), "production switchboard-tasks.service present")
unit_text = unit.read_text(encoding="utf-8")
ok("switchboard.services.tasks.app:create_app" in unit_text, "unit factory entrypoint")
ok("8122" in unit_text, "unit listens on :8122")

mono = (ROOT / "deploy" / "projectplanner.service").read_text(encoding="utf-8")
ok("PM_TASKS_HTTP_PRIMARY=service" in mono, "monolith dual-strip env set")

app_impl_src = entrypoint_source("app")
ok("PM_TASKS_HTTP_PRIMARY" in app_impl_src, "app_impl gates Tasks mount on dual-strip")
ok("sibling_bc_only" in app_impl_src, "app_impl mounts sibling_bc_only when service primary")

# --- hermetic dual-strip behavior -------------------------------------------
os.environ["PM_TASKS_HTTP_PRIMARY"] = "service"

# Fresh import of app after env set.
import store  # noqa: E402

store.init_project_registry()
store.create_project("MS92", project_id="ms92-alpha", actor="test")
store.init_db("ms92-alpha")

# Import app_impl under dual-strip — use TestClient against create_task paths.
# Re-load by importing app which pulls app_impl (already may be cached). Prefer
# explicit router build matching production branch.
from switchboard.api import deps  # noqa: E402
from switchboard.api.routers import claims as claims_router  # noqa: E402
from switchboard.api.routers import tasks as tasks_router  # noqa: E402
from switchboard.api.tasks_port_adapters import configure_tasks_ports, ensure_tasks_runtime  # noqa: E402
from fastapi import FastAPI  # noqa: E402

configure_tasks_ports()
ensure_tasks_runtime()
stripped = FastAPI()
stripped.include_router(tasks_router.create_router(
    resolve_project=deps.resolve_project,
    resolve_principal=deps.resolve_principal,
    sibling_bc_only=True,
))
# claims intentionally omitted under dual-strip
client = TestClient(stripped)

ok(client.get("/api/tasks", params={"project": "ms92-alpha"}).status_code == 404,
   "dual-strip monolith omits Mode A list")
disp = client.post("/api/tasks/x/dispatch", json={"project": "ms92-alpha"})
ok(
    disp.status_code != 404
    or isinstance(disp.json() if disp.headers.get("content-type", "").startswith("application/json") else None, (dict, list)),
    f"dual-strip monolith keeps dispatch (status={disp.status_code})",
)
# Prefer: miss-task 404 from handler still proves mount (JSON body), not FastAPI miss.
ok(
    "task not found" in str(disp.json() if disp.status_code == 404 else {})
    or disp.status_code != 404,
    "dispatch miss is handler 404 (mounted) not missing route",
)
execution = client.get("/api/tasks/x/execution", params={"project": "ms92-alpha"})
ok(
    execution.status_code != 404
    or "task_not_found" in str(execution.json()),
    "task execution miss is a command refusal (mounted) not a missing route",
)

# Cut still serves Mode A
from switchboard.services.tasks import create_app  # noqa: E402
from switchboard.services.tasks.settings import TasksServiceSettings  # noqa: E402
cut = TestClient(create_app(TasksServiceSettings(
    service_name="arch-ms92", host="127.0.0.1", port=8122,
)))
ok(cut.get("/health").status_code == 200, "Tasks cut /health ok")
ok(cut.get("/api/tasks", params={"project": "ms92-alpha"}).status_code == 200,
   "Tasks cut still lists Mode A")
ok(cut.post("/api/tasks/x/dispatch", json={"project": "ms92-alpha"}).status_code == 404,
   "Tasks cut still omits dispatch")
ok(cut.get("/api/tasks/x/execution", params={"project": "ms92-alpha"}).status_code == 404,
   "Tasks cut still omits task execution")

# --- independence / exit gate Path A ----------------------------------------
import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

verdict = json.loads((ROOT / "docs/phase3/tasks_independence_verdict.json").read_text())
ok(str(verdict.get("verdict") or "").lower() == "go", "independence verdict is go")
ok(verdict.get("inputs", {}).get("G6_operator_go") is True, "operator G6 recorded")
ok(verdict.get("process_cut_authorized") is True, "process cut authorized")

playbook = (ROOT / "docs/phase3/tasks_cut_playbook.md").read_text(encoding="utf-8")
ok("Path A" in playbook or "live" in playbook.lower(), "cut playbook documents Path A live")
runbook = (ROOT / "docs/runbooks/tasks-caddy-cutover-rollback.md").read_text(encoding="utf-8")
ok("Rollback" in runbook and "8122" in runbook, "rollback runbook still present")

proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "arch_ms_phase3_exit_gate.py")],
    cwd=ROOT, text=True, capture_output=True,
)
try:
    report = json.loads(proc.stdout)
except json.JSONDecodeError:
    report = {"passed": False, "error": proc.stdout or proc.stderr}
ok(bool(report.get("passed")), f"phase3 exit gate passed=true ({report.get('error')})")
ok(bool(report.get("paths", {}).get("path_a_tasks_cut")), "Path A Tasks cut satisfied")
ok(bool(report.get("checks", {}).get("no_half_cut_network_facade")),
   "no half-cut façade under authorized Go")

# sibling_bc_only mutual exclusion
try:
    tasks_router.create_router(
        resolve_project=deps.resolve_project,
        resolve_principal=deps.resolve_principal,
        thin_mode_a=True,
        sibling_bc_only=True,
    )
    ok(False, "thin_mode_a + sibling_bc_only must raise")
except ValueError:
    ok(True, "thin_mode_a + sibling_bc_only mutually exclusive")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
