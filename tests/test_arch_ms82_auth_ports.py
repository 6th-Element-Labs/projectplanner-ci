#!/usr/bin/env python3
"""ARCH-MS-82: auth package ports — no forbidden monolith imports + protocol wiring."""
from __future__ import annotations

import ast
import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms82-auth-ports-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms82"
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0

FORBIDDEN_ROOT_MODULES = frozenset({
    "store",
    "auth",
    "notify",
    "app_impl",
    "mcp_server",
    "mcp_server_impl",
})

AUTH_PKG = ROOT / "src" / "switchboard" / "api" / "routers" / "auth"


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
for name in ("ports.py", "deps.py", "service.py", "store.py", "routes.py"):
    ok((AUTH_PKG / name).is_file(), f"auth/{name} present")

ok((ROOT / "src/switchboard/api/auth_port_adapters.py").is_file(),
   "auth_port_adapters.py lives outside the auth package")

# --- import lint: no forbidden root modules in auth package ------------------
for path in sorted(AUTH_PKG.glob("*.py")):
    hits = _forbidden_imports(path)
    ok(not hits, f"{path.name}: no forbidden monolith imports"
       + (f" (found {hits})" if hits else ""))

# --- ports + adapters import cleanly -----------------------------------------
from switchboard.api.auth_port_adapters import (  # noqa: E402
    MonolithAuthRegistry,
    Pbkdf2PasswordHasher,
    SmtpAuthNotifier,
    configure_auth_ports,
)
from switchboard.api.routers.auth import deps as auth_deps  # noqa: E402
from switchboard.api.routers.auth.ports import (  # noqa: E402
    AuthNotifier,
    AuthRegistry,
    PasswordHasher,
)

configure_auth_ports()
ok(isinstance(Pbkdf2PasswordHasher(), PasswordHasher),
   "Pbkdf2PasswordHasher satisfies PasswordHasher")
ok(isinstance(SmtpAuthNotifier(), AuthNotifier),
   "SmtpAuthNotifier satisfies AuthNotifier")
ok(isinstance(MonolithAuthRegistry(), AuthRegistry),
   "MonolithAuthRegistry satisfies AuthRegistry")
ok(auth_deps.is_configured(), "configure_auth_ports binds auth deps")

# --- behavior parity: register / login / session via ports -------------------
import store  # noqa: E402
from switchboard.api.routers.auth import service as auth_service  # noqa: E402
from switchboard.api.routers.auth import store as auth_store  # noqa: E402

store.init_project_registry()
auth_store.init()
store.create_project("Alpha", project_id="alpha", actor="test")

user, token, exp = auth_service.register(
    "ports@example.com", "Ports", "password123", ip="127.0.0.1")
ok(user["email"] == "ports@example.com", "register returns public user")
ok(isinstance(token, str) and len(token) > 20, "register issues session token")
ok(user.get("projects") == [], "new user has deny-by-default empty projects")

session_user = auth_service.current_user(token)
ok(session_user is not None and session_user["email"] == "ports@example.com",
   "current_user resolves session")

login_user, login_token, _ = auth_service.login(
    "ports@example.com", "password123", ip="127.0.0.1")
ok(login_user["email"] == "ports@example.com", "login succeeds with same password")
ok(auth_service.current_user(login_token) is not None, "login session verifies")

# hashed with PBKDF2 wire format
account = auth_store.get_user_by_email("ports@example.com")
ok(bool(account and str(account.get("password_hash") or "").startswith("pbkdf2_sha256$")),
   "password hash uses pbkdf2_sha256 wire format (not bcrypt)")

# design doc no longer claims bcrypt as current reality
design = (ROOT / "docs/AUTH-MICROSERVICE-DESIGN.md").read_text(encoding="utf-8")
ok("PBKDF2" in design or "pbkdf2" in design, "AUTH-MICROSERVICE-DESIGN mentions PBKDF2")
ok("password_hash bcrypt" not in design,
   "AUTH-MICROSERVICE-DESIGN no longer lists password_hash bcrypt as the model")

print(f"\narch_ms82_auth_ports: {passed} passed, {failed} failed")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if failed else 0)
