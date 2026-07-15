#!/usr/bin/env python3
"""ARCH-MS-83: Auth ownership + production JWT secret fail-fast + independence gate doc."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

TMP = tempfile.mkdtemp(prefix="arch-ms83-auth-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms83"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


# --- docs present ------------------------------------------------------------
gate = ROOT / "docs" / "AUTH-INDEPENDENCE-GATE.md"
ok(gate.is_file(), "docs/AUTH-INDEPENDENCE-GATE.md exists")
gate_text = gate.read_text(encoding="utf-8") if gate.is_file() else ""
ok("Fail-closed" in gate_text or "fail-closed" in gate_text.lower(),
   "independence gate documents fail-closed Auth-down")
ok("PM_JWT_SECRET" in gate_text, "independence gate documents PM_JWT_SECRET fail-fast")
ok("Go / No-Go" in gate_text or "Go/No-Go" in gate_text, "independence gate has Go/No-Go checklist")
ok("ensure_identity" in gate_text or "Exclusive writer" in gate_text,
   "independence gate documents exclusive writers")

design = (ROOT / "docs" / "AUTH-MICROSERVICE-DESIGN.md").read_text(encoding="utf-8")
ok("AUTH-INDEPENDENCE-GATE" in design, "AUTH-MICROSERVICE-DESIGN links independence gate")
ok("fail-fast" in design.lower() or "PM_JWT_SECRET" in design,
   "AUTH-MICROSERVICE-DESIGN mentions secrets fail-fast")

# --- secrets fail-fast -------------------------------------------------------
from switchboard.api.routers.auth import session as auth_session  # noqa: E402

# required mode, no secret → raise
os.environ["PM_AUTH_MODE"] = "required"
os.environ.pop("PM_JWT_SECRET", None)
os.environ.pop("PM_AUTH_TOKEN", None)
raised = False
try:
    auth_session._secret()
except auth_session.AuthSecretError:
    raised = True
ok(raised, "required mode without PM_JWT_SECRET raises AuthSecretError")

# required mode must not accept PM_AUTH_TOKEN alone
os.environ["PM_AUTH_TOKEN"] = "not-a-jwt-secret"
raised = False
try:
    auth_session._secret()
except auth_session.AuthSecretError:
    raised = True
ok(raised, "required mode refuses PM_AUTH_TOKEN substitute")
os.environ.pop("PM_AUTH_TOKEN", None)

os.environ["PM_JWT_SECRET"] = "prod-secret-value"
ok(auth_session._secret() == "prod-secret-value",
   "required mode returns PM_JWT_SECRET when set")
ok(auth_session.require_production_secret() == "prod-secret-value",
   "require_production_secret resolves when configured")

# DEV_OPEN allows fallback
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ.pop("PM_JWT_SECRET", None)
os.environ.pop("PM_AUTH_TOKEN", None)
ok(auth_session._secret() == auth_session._DEV_JWT_FALLBACK,
   "dev-open allows explicit DEV JWT fallback")

# --- exclusive users writer via Auth ensure_identity -------------------------
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms83"

from switchboard.api.auth_port_adapters import configure_auth_ports  # noqa: E402
import store  # noqa: E402
from switchboard.api.routers.auth import store as auth_store  # noqa: E402

configure_auth_ports()
store.init_project_registry()
auth_store.init()

row = auth_store.ensure_identity("user-archms83", email="arch83@example.com",
                                 display_name="Arch 83")
ok(row["id"] == "user-archms83", "ensure_identity creates users row")
ok(row.get("email") == "arch83@example.com", "ensure_identity stores email")

# Access.ensure_user must delegate (no direct second writer)
via_access = store.ensure_user("user-archms83b", email="b@example.com",
                               display_name="B", created_by="test")
ok(via_access["id"] == "user-archms83b", "store.ensure_user delegates to Auth identity")

# verify still requires DB session row (fail-closed vs offline JWT)
from switchboard.api.routers.auth import service as auth_service  # noqa: E402

user, token, _ = auth_service.register(
    "session83@example.com", "S83", "password123", ip="127.0.0.1")
ok(auth_service.current_user(token) is not None, "verify succeeds with live session row")
# Corrupt by revoking — JWT may still decode, but verify must fail closed
auth_service.logout(token)
ok(auth_service.current_user(token) is None,
   "revoked session fails closed (JWT alone insufficient)")

print(f"\narch_ms83_auth_ownership: {passed} passed, {failed} failed")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(1 if failed else 0)
