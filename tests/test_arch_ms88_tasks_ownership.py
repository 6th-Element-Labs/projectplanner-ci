#!/usr/bin/env python3
"""ARCH-MS-88: Tasks ownership + fail-closed write-binding + independence gate doc."""
from __future__ import annotations

import ast
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms88-tasks-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms88"

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
                continue
            mod = node.module or ""
            root = mod.split(".", 1)[0]
            if root in FORBIDDEN_ROOT_MODULES:
                found.append(f"from {mod} import …")
    return found


# --- docs present ------------------------------------------------------------
gate = ROOT / "docs" / "TASKS-INDEPENDENCE-GATE.md"
ok(gate.is_file(), "docs/TASKS-INDEPENDENCE-GATE.md exists")
gate_text = gate.read_text(encoding="utf-8") if gate.is_file() else ""
ok("Exclusive writer" in gate_text or "exclusive writer" in gate_text.lower(),
   "independence gate documents exclusive writers")
ok("shared-SQLite" in gate_text or "shared SQLite" in gate_text.lower()
   or "Shared project SQLite" in gate_text,
   "independence gate documents shared-SQLite policy")
ok("fail-closed" in gate_text.lower() or "Fail closed" in gate_text,
   "independence gate documents fail-closed Auth/write-binding")
ok("Go / No-Go" in gate_text or "Go/No-Go" in gate_text,
   "independence gate has Go/No-Go checklist")
ok("require_write_binding" in gate_text,
   "independence gate names require_write_binding")
ok("TaskWriteBindingPort" in gate_text or "write-binding via ports" in gate_text.lower(),
   "independence gate requires Auth binding via ports")

adr = (ROOT / "docs" / "decisions" / "0012-phase3-tasks-process-strangler.md").read_text(
    encoding="utf-8")
ok("TASKS-INDEPENDENCE-GATE" in adr, "ADR-0012 links TASKS-INDEPENDENCE-GATE")

# --- package files + import lint ---------------------------------------------
ok((TASKS_PKG / "binding.py").is_file(), "services/tasks/binding.py present")
for path in sorted(TASKS_PKG.glob("*.py")):
    hits = _forbidden_imports(path)
    ok(not hits, f"{path.name}: no forbidden monolith imports"
       + (f" (found {hits})" if hits else ""))

# --- fail-closed write-binding via ports -------------------------------------
from switchboard.api.tasks_port_adapters import configure_tasks_ports  # noqa: E402
import store  # noqa: E402
from switchboard.services.tasks import binding as tasks_binding  # noqa: E402
from switchboard.services.tasks import deps as tasks_deps  # noqa: E402

configure_tasks_ports()
store.init_project_registry()
store.create_project("Switchboard", project_id="switchboard", actor="test")
store.init_db("switchboard")

# Bound principal succeeds
bound = tasks_binding.require_write_binding(
    "cursor/agent-ms88", project="switchboard", task_id="ARCH-MS-88")
ok(bound.get("ok") is True and bound.get("actor") == "cursor/agent-ms88",
   "require_write_binding accepts a named agent actor")

# Naked shared env token without agent_id / system_actor fails closed
raised = False
payload = {}
try:
    tasks_binding.require_write_binding(
        "env-mcp-token", project="switchboard", task_id="ARCH-MS-88")
except tasks_binding.WriteBindingError as exc:
    raised = True
    payload = dict(exc.payload)
ok(raised, "naked env-mcp-token raises WriteBindingError")
ok(payload.get("failure_class") == "unbound_identity",
   "unbound env token failure_class is unbound_identity")
ok(payload.get("ok") is False, "denied binding payload has ok=false")

# Explicit system actor + reason succeeds
sys_bound = tasks_binding.require_write_binding(
    "env-mcp-token",
    project="switchboard",
    task_id="ARCH-MS-88",
    system_actor="switchboard/arch-ms88-test",
    system_reason="ARCH-MS-88 fail-closed binding proof",
)
ok(sys_bound.get("ok") is True
   and sys_bound.get("actor") == "switchboard/arch-ms88-test",
   "system_actor + system_reason binds explicitly")

# Principal port mapping
actor = tasks_binding.principal_actor({
    "id": "agent/ms88",
    "display_name": "agent/ms88",
    "kind": "agent",
})
ok(actor == "agent/ms88", "principal_actor uses TaskPrincipalPort")

# Activity payload via port
activity = tasks_binding.activity_payload_for_binding(sys_bound)
ok(isinstance(activity, dict) and activity.get("actor") == "switchboard/arch-ms88-test",
   "activity_payload_for_binding uses write-binding port")

ok(tasks_deps.is_configured(), "tasks deps remain configured after binding calls")

print(f"\narch_ms88_tasks_ownership: {passed} passed, {failed} failed")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if failed else 0)
