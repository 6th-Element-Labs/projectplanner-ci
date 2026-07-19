#!/usr/bin/env python3
"""ARCH-MS-120: standalone Ingest service, retry, Auth, and boundary proof."""
from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms120-ingest-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "arch-ms120-test-secret"

passed = failed = 0


def ok(value, message):
    global passed, failed
    print(("  PASS  " if value else "  FAIL  ") + message)
    passed += int(bool(value)); failed += int(not value)


package = ROOT / "src/switchboard/services/ingest"
for path in package.glob("*.py"):
    tree = ast.parse(path.read_text())
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.append(node.module.split(".", 1)[0])
    ok(not ({"store", "intake", "inbox_store", "auth", "app_impl"} & set(roots)),
       f"{path.name} has no forbidden monolith imports")

unit = (ROOT / "deploy/ingest/switchboard-ingest.service.example").read_text()
ok("MemoryMax=64M" in unit and "SWITCHBOARD_INGEST_PORT=8126" in unit,
   "side-by-side unit pins port 8126 and the proven memory cap")

from db.connection import _conn
from db.schema import apply_schema, init_project_registry
from switchboard.api.ingest_port_adapters import ProjectScopedIngestAuth, RepositoryIngest
from switchboard.services.ingest import deps
from switchboard.services.ingest.router import create_router
from switchboard.services.ingest.health import create_router as create_health_router

init_project_registry()
with _conn("switchboard") as connection:
    apply_schema(connection)
with _conn("helm") as connection:
    apply_schema(connection)
calls = []


def fake_intake(kind, title, text, project=None, **_kwargs):
    calls.append((kind, title, text, project))
    return {"summary": "triaged", "proposals": [], "new_tasks": [], "sources": [],
            "ingested_chunks": 1}


repo = RepositoryIngest(executor=fake_intake)
body = {"kind": "note", "title": "Parity", "text": "hello"}
first = repo.intake("switchboard", body, "key-1")
second = repo.intake("switchboard", body, "key-1")
ok(first == second and len(calls) == 1, "same key replays without duplicate effects")
repo.intake("helm", body, "key-1")
ok(len(calls) == 2 and calls[-1][-1] == "helm",
   "operation keys and writes remain isolated per project database")
try:
    repo.intake("switchboard", {**body, "text": "changed"}, "key-1")
    conflict = False
except ValueError:
    conflict = True
ok(conflict, "same key with changed payload fails closed")


class OpenAuth:
    def authorize(self, request, project, scopes):
        return {"project": project, "scopes": scopes}


auth_calls = []
auth_port = ProjectScopedIngestAuth(
    resolver=lambda request, project, scopes, dev_actor: auth_calls.append(
        (project, scopes, dev_actor)) or {"project": project}
)
auth_port.authorize(object(), "switchboard", ("write",))
ok(auth_calls == [("switchboard", ("write",), "ingest")],
   "standalone Auth port preserves project and required scope")


deps.configure(ingest=repo, auth=OpenAuth())
app = FastAPI()
app.include_router(create_health_router(service_name="arch-ms120-test", readiness_probe=lambda: {"ok": True, "checks": {"database_schema": "ok"}}))
app.include_router(create_router(resolve_project=lambda value: value))
client = TestClient(app)
ok(client.get("/health").json() == {"status": "ok", "service": "arch-ms120-test"},
   "standalone health endpoint identifies service")
ok(client.post("/api/intake", params={"project": "switchboard"}, json=body).status_code == 422,
   "text intake requires Idempotency-Key")
response = client.post("/api/intake", params={"project": "switchboard"}, json=body,
                       headers={"Idempotency-Key": "key-2"})
ok(response.status_code == 200 and response.json()["summary"] == "triaged",
   "standalone text intake response parity")
inbox = client.get("/api/inbox", params={"project": "switchboard"})
ok(inbox.status_code == 200 and set(inbox.json()) == {"items", "pending"},
   "standalone inbox read shape parity")
ok(client.get("/api/inbox").status_code == 422, "project is required")

print(f"\nARCH-MS-120 Ingest service: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
