#!/usr/bin/env python3
"""ARCH-MS-75: Auth standalone uvicorn — side-by-side process cut (pre-Caddy)."""
from __future__ import annotations

import ast
import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms75-auth-svc-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms75"
Path(os.environ["PM_DYNAMIC_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

passed = failed = 0

FORBIDDEN_ROOT_MODULES = frozenset({
    "store",
    "auth",
    "notify",
    "app_impl",
    "mcp_server",
    "mcp_server_impl",
})
AUTH_SVC = ROOT / "src" / "switchboard" / "services" / "auth"
# Composition root may import auth_port_adapters only.
ADAPTER_ALLOW = {"switchboard.api.auth_port_adapters"}


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
    "switchboard.services.auth",
    "switchboard.services.auth.settings",
    "switchboard.services.auth.health",
    "switchboard.services.auth.app",
):
    try:
        importlib.import_module(name)
        ok(True, f"import {name}")
    except Exception as exc:
        ok(False, f"import {name}: {exc}")

# --- import lint -------------------------------------------------------------
for path in sorted(AUTH_SVC.glob("*.py")):
    allow = path.name == "app.py"
    hits = _forbidden_imports(path, allow_adapters=allow)
    ok(not hits, f"{path.name}: no forbidden monolith imports"
       + (f" (found {hits})" if hits else ""))

# --- health + auth parity via Auth app ---------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from switchboard.services.auth import create_app  # noqa: E402
from switchboard.services.auth.settings import AuthServiceSettings  # noqa: E402
import store  # noqa: E402
from switchboard.storage.repositories import access  # noqa: E402

store.init_project_registry()
access.ensure_org("org-arch-ms75", name="ARCH-MS-75 Org", slug="arch-ms75")

settings = AuthServiceSettings(
    service_name="arch-ms75-test",
    host="127.0.0.1",
    port=8121,
)
client = TestClient(create_app(settings))

health = client.get("/health")
ok(health.status_code == 200, f"/health status {health.status_code}")
ok(health.json().get("status") == "ok", "/health status=ok")
ok(health.json().get("service") == "arch-ms75-test", "/health service name")

reg = client.post("/api/auth/register", json={
    "email": "ms75@example.com",
    "display_name": "MS75",
    "password": "password12345",
})
ok(reg.status_code == 200, f"register status {reg.status_code}")
ok((reg.json() or {}).get("user", {}).get("email") == "ms75@example.com",
   "register returns user")

sess = client.get("/api/auth/session")
ok(sess.status_code == 200, f"session status {sess.status_code}")
user = (sess.json() or {}).get("user") or {}
ok(user.get("email") == "ms75@example.com", "session user email")
ok(isinstance(user.get("projects"), list), "session user.projects is a list (grants surface)")

# Seed a project grant via Access (Auth only reads for session.projects).
uid = user.get("id")
store.create_project("MS75 Alpha", project_id="ms75-alpha", actor="test")
if uid:
    access.grant_project_role("ms75-alpha", "user", uid, "admin")
sess2 = client.get("/api/auth/session")
projects = ((sess2.json() or {}).get("user") or {}).get("projects") or []
ok(any(
    (isinstance(p, dict) and p.get("id") == "ms75-alpha")
    or p == "ms75-alpha"
    for p in projects
), f"session reflects Access grant (projects={projects!r})")

login = client.post("/api/auth/login", json={
    "email": "ms75@example.com", "password": "password12345",
})
ok(login.status_code == 200, f"login status {login.status_code}")

bad = client.post("/api/auth/login", json={
    "email": "ms75@example.com", "password": "wrong-password-xx",
})
ok(bad.status_code == 401, f"bad login is 401 (got {bad.status_code})")

logout = client.post("/api/auth/logout")
ok(logout.status_code == 200, f"logout status {logout.status_code}")
after = client.get("/api/auth/session")
ok(after.status_code == 401, f"session after logout is 401 (got {after.status_code})")

# --- façade + package surface (Caddy cut lives in ARCH-MS-76) ---------------
app_impl_src = entrypoint_source("app")
ok(
    "switchboard.services.auth" not in app_impl_src
    or "services.auth" not in app_impl_src.replace("services.auth_port", ""),
    "live app_impl does not import services.auth process package",
)
ok("switchboard.services.auth" not in app_impl_src,
   "app_impl does not reference switchboard.services.auth")
ok("_global_auth_router" in app_impl_src or "routers.auth" in app_impl_src
   or "auth.routes" in app_impl_src,
   "monolith still mounts Auth router (rollback green façade)")

unit = ROOT / "deploy" / "auth" / "switchboard-auth.service.example"
readme = ROOT / "deploy" / "auth" / "README.md"
frag = ROOT / "deploy" / "skeleton" / "Caddyfile.auth-fragment.example"
ok(unit.is_file(), "deploy/auth/switchboard-auth.service.example exists")
ok(readme.is_file(), "deploy/auth/README.md exists")
ok(frag.is_file(), "Caddyfile.auth-fragment.example retained as drill reference")
unit_text = unit.read_text(encoding="utf-8")
ok("switchboard.services.auth.app:create_app" in unit_text
   or "switchboard.services.auth.app:app" in unit_text,
   "systemd example points at Auth uvicorn app")
ok("8121" in unit_text, "systemd example uses port 8121")

gate = (ROOT / "docs" / "AUTH-INDEPENDENCE-GATE.md").read_text(encoding="utf-8")
ok("G6" in gate and ("Go" in gate or "operator" in gate.lower()),
   "independence gate still documents G6")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
