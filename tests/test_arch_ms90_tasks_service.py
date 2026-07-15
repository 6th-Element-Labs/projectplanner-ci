#!/usr/bin/env python3
"""ARCH-MS-90: Tasks standalone uvicorn — side-by-side process cut (pre-Caddy)."""
from __future__ import annotations

import ast
import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms90-tasks-svc-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms90"
Path(os.environ["PM_DYNAMIC_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

passed = failed = 0

FORBIDDEN_ROOT_MODULES = frozenset({
    "store",
    "auth",
    "notify",
    "dispatch",
    "app_impl",
    "mcp_server",
    "mcp_server_impl",
})
TASKS_SVC = ROOT / "src" / "switchboard" / "services" / "tasks"
# Composition root may import tasks_port_adapters only.
ADAPTER_ALLOW = {"switchboard.api.tasks_port_adapters"}


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _forbidden_imports(path: Path, *, allow_adapters: bool = False) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in FORBIDDEN_ROOT_MODULES:
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            mod = node.module or ""
            if allow_adapters and mod in ADAPTER_ALLOW:
                continue
            root = mod.split(".", 1)[0]
            if root in FORBIDDEN_ROOT_MODULES:
                found.append(f"from {mod} import …")
    return found


# --- package surface ---------------------------------------------------------
for name in (
    "switchboard.services.tasks",
    "switchboard.services.tasks.settings",
    "switchboard.services.tasks.health",
    "switchboard.services.tasks.app",
):
    try:
        importlib.import_module(name)
        ok(True, f"import {name}")
    except Exception as exc:
        ok(False, f"import {name}: {exc}")

# --- import lint -------------------------------------------------------------
for path in sorted(TASKS_SVC.glob("*.py")):
    allow = path.name == "app.py"
    hits = _forbidden_imports(path, allow_adapters=allow)
    ok(not hits, f"{path.name}: no forbidden monolith imports"
       + (f" (found {hits})" if hits else ""))

# --- health + Mode A surface via Tasks app -----------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from switchboard.services.tasks import create_app  # noqa: E402
from switchboard.services.tasks.settings import TasksServiceSettings  # noqa: E402
import store  # noqa: E402

store.init_project_registry()
store.create_project("MS90 Alpha", project_id="ms90-alpha", actor="test")
store.init_db("ms90-alpha")

settings = TasksServiceSettings(
    service_name="arch-ms90-test",
    host="127.0.0.1",
    port=8122,
)
client = TestClient(create_app(settings))

health = client.get("/health")
ok(health.status_code == 200, f"/health status {health.status_code}")
ok(health.json().get("status") == "ok", "/health status=ok")
ok(health.json().get("service") == "arch-ms90-test", "/health service name")

listing = client.get("/api/tasks", params={"project": "ms90-alpha"})
ok(listing.status_code == 200, f"list tasks status {listing.status_code}")
ok(isinstance((listing.json() or {}).get("tasks"), list), "list tasks returns tasks[]")

created = client.post(
    "/api/tasks",
    params={"project": "ms90-alpha"},
    json={"workstream_id": "ARCH-MS", "title": "ms90 create"},
)
ok(created.status_code == 200, f"create task status {created.status_code}")
task_id = (created.json() or {}).get("task_id") or ""
ok(bool(task_id), f"create returns task_id ({task_id!r})")

if task_id:
    got = client.get(f"/api/tasks/{task_id}", params={"project": "ms90-alpha"})
    ok(got.status_code == 200, f"get task status {got.status_code}")
    ok((got.json() or {}).get("task_id") == task_id, "get returns same task_id")

# Mode A — sibling BC routes must NOT be mounted on :8122 surface
sibling_404 = client.post(
    f"/api/tasks/{task_id or 'x'}/dispatch",
    json={"project": "ms90-alpha"},
)
ok(sibling_404.status_code == 404, f"dispatch omitted on thin Mode A (got {sibling_404.status_code})")

chat_404 = client.post(
    f"/api/tasks/{task_id or 'x'}/chat",
    params={"project": "ms90-alpha"},
    json={"message": "hello"},
)
ok(chat_404.status_code == 404, f"chat omitted on thin Mode A (got {chat_404.status_code})")

review_404 = client.get(
    f"/api/tasks/{task_id or 'x'}/review_verdict",
    params={"project": "ms90-alpha"},
)
ok(review_404.status_code == 404, f"review omitted on thin Mode A (got {review_404.status_code})")

# TXP claims surface is mounted (unauthenticated shape still 4xx, not 404)
claim_next = client.post("/txp/v1/claim_next", json={
    "agent_id": "arch-ms90-agent", "project": "ms90-alpha",
})
ok(claim_next.status_code != 404, f"claim_next mounted (status {claim_next.status_code})")

# --- deploy surface (example only; no live Caddy / unit yet) -----------------
app_impl_src = entrypoint_source("app")
ok("switchboard.services.tasks" not in app_impl_src,
   "app_impl does not reference switchboard.services.tasks process package")
ok("PM_TASKS_HTTP_PRIMARY" not in app_impl_src,
   "monolith does not yet dual-strip Tasks (ARCH-MS-92)")

unit = ROOT / "deploy" / "tasks" / "switchboard-tasks.service.example"
frag = ROOT / "deploy" / "skeleton" / "Caddyfile.tasks-fragment.example"
ok(unit.is_file(), "deploy/tasks/switchboard-tasks.service.example exists")
ok(frag.is_file(), "Caddyfile.tasks-fragment.example retained as drill reference")
unit_text = unit.read_text(encoding="utf-8")
ok("switchboard.services.tasks.app:create_app" in unit_text,
   "systemd example points at Tasks uvicorn app")
ok("8122" in unit_text, "systemd example uses port 8122")
ok("/bin/false" not in unit_text.split("ExecStart=")[-1].split("\n")[0]
   or "create_app" in unit_text,
   "systemd example no longer uses placeholder ExecStart=/bin/false alone")
# Prefer active ExecStart (not only commented)
ok(
    any(
        line.startswith("ExecStart=") and "create_app" in line and not line.strip().startswith("#")
        for line in unit_text.splitlines()
    ),
    "active ExecStart runs Tasks create_app factory",
)

gate = (ROOT / "docs" / "TASKS-INDEPENDENCE-GATE.md").read_text(encoding="utf-8")
ok("G6" in gate and ("Go" in gate or "operator" in gate.lower()),
   "independence gate still documents G6")

# default port
ok(TasksServiceSettings.from_env().port == 8122, "default Tasks port is 8122")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
