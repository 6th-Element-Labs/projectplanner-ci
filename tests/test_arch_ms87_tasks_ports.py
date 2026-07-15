#!/usr/bin/env python3
"""ARCH-MS-87: Tasks service package ports — no forbidden monolith imports + protocol wiring."""
from __future__ import annotations

import ast
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms87-tasks-ports-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms87"
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0

FORBIDDEN_ROOT_MODULES = frozenset({
    "store",
    "auth",
    "notify",
    "dispatch",
    "agent",
    "app_impl",
    "mcp_server",
    "mcp_server_impl",
})

TASKS_PKG = ROOT / "src" / "switchboard" / "services" / "tasks"


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _forbidden_imports(path: Path) -> list[str]:
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
                continue  # relative imports stay inside the package
            mod = node.module or ""
            root = mod.split(".", 1)[0]
            if root in FORBIDDEN_ROOT_MODULES:
                found.append(f"from {mod} import …")
    return found


# --- package files exist -----------------------------------------------------
for name in ("ports.py", "deps.py", "__init__.py"):
    ok((TASKS_PKG / name).is_file(), f"services/tasks/{name} present")

ok((ROOT / "src/switchboard/api/tasks_port_adapters.py").is_file(),
   "tasks_port_adapters.py lives outside the Tasks package")

ok("configure_tasks_ports" in (ROOT / "app_impl.py").read_text(encoding="utf-8"),
   "app_impl wires configure_tasks_ports")

# --- import lint: no forbidden root modules in Tasks package -----------------
for path in sorted(TASKS_PKG.glob("*.py")):
    hits = _forbidden_imports(path)
    ok(not hits, f"{path.name}: no forbidden monolith imports"
       + (f" (found {hits})" if hits else ""))

# --- ports + adapters import cleanly -----------------------------------------
from switchboard.api.tasks_port_adapters import (  # noqa: E402
    AuthTaskPrincipal,
    MonolithClaimLifecycle,
    MonolithTaskBoard,
    MonolithTaskWriteBinding,
    MonolithWorkSessionLookup,
    configure_tasks_ports,
)
from switchboard.services.tasks import deps as tasks_deps  # noqa: E402
from switchboard.services.tasks.ports import (  # noqa: E402
    ClaimLifecyclePort,
    TaskBoardPort,
    TaskPrincipalPort,
    TaskWriteBindingPort,
    WorkSessionLookupPort,
)

configure_tasks_ports()
ok(isinstance(AuthTaskPrincipal(), TaskPrincipalPort),
   "AuthTaskPrincipal satisfies TaskPrincipalPort")
ok(isinstance(MonolithTaskWriteBinding(), TaskWriteBindingPort),
   "MonolithTaskWriteBinding satisfies TaskWriteBindingPort")
ok(isinstance(MonolithTaskBoard(), TaskBoardPort),
   "MonolithTaskBoard satisfies TaskBoardPort")
ok(isinstance(MonolithClaimLifecycle(), ClaimLifecyclePort),
   "MonolithClaimLifecycle satisfies ClaimLifecyclePort")
ok(isinstance(MonolithWorkSessionLookup(), WorkSessionLookupPort),
   "MonolithWorkSessionLookup satisfies WorkSessionLookupPort")
ok(tasks_deps.is_configured(), "configure_tasks_ports binds tasks deps")

# --- behavior smoke: principal + board list via ports ------------------------
import store  # noqa: E402

store.init_project_registry()
store.create_project("Switchboard", project_id="switchboard", actor="test")
store.init_db("switchboard")

actor = tasks_deps.principal().actor({
    "id": "agent/test",
    "display_name": "agent/test",
    "kind": "agent",
})
ok(actor == "agent/test", "principal port resolves actor display_name")

binding = tasks_deps.write_binding().resolve_write_actor(
    "agent/test", project="switchboard", task_id="ARCH-MS-87")
ok(isinstance(binding, dict) and binding.get("actor"),
   "write_binding port returns binding dict")

payload = tasks_deps.write_binding().write_binding_activity_payload(binding)
ok(isinstance(payload, dict) and "actor" in payload,
   "write_binding activity payload normalizes binding")

rows = tasks_deps.board().list_tasks(project="switchboard")
ok(isinstance(rows, list), "board port list_tasks returns a list")

missing = tasks_deps.work_sessions().get_work_session(
    "ws-does-not-exist", project="switchboard")
ok(missing is None, "work_session port returns None for missing id")

# --- architecture ratchet includes tasks forbidden imports -------------------
baseline = ROOT / "perf" / "arch_ms84_ratchet_baseline.json"
ok(baseline.is_file(), "arch_ms84 ratchet baseline exists")
import json  # noqa: E402
scopes = json.loads(baseline.read_text(encoding="utf-8")).get("scopes") or {}
ok("tasks_forbidden_imports" in scopes, "baseline scopes tasks_forbidden_imports")
ok(int(scopes["tasks_forbidden_imports"].get("ceiling", 1)) == 0,
   "tasks_forbidden_imports ceiling is 0")

print(f"\narch_ms87_tasks_ports: {passed} passed, {failed} failed")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if failed else 0)
