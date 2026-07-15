#!/usr/bin/env python3
"""ARCH-MS-77: Auth cutover parity (Go path) + dual-mount strip ratchet.

Hermetic: same temp Auth DB exercised via (1) in-process Auth router baseline
and (2) ``switchboard.services.auth.create_app``. Assert status + key JSON
parity for register / login / session / logout, grants, superadmin, 401/403.

Optional live smoke: set ``ARCH_MS77_LIVE_SMOKE=1`` and optionally
``ARCH_MS77_BASE_URL`` (default https://plan.taikunai.com).
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from path_setup import ROOT, entrypoint_source

TMP = tempfile.mkdtemp(prefix="arch-ms77-auth-parity-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = str(Path(TMP) / "projects")
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ["PM_JWT_SECRET"] = "test-secret-arch-ms77"
Path(os.environ["PM_DYNAMIC_PROJECTS_DIR"]).mkdir(parents=True, exist_ok=True)

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def _session_user(resp) -> dict[str, Any]:
    return ((resp.json() or {}).get("user") or {}) if resp.status_code == 200 else {}


def _project_ids(user: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for p in user.get("projects") or []:
        if isinstance(p, dict) and p.get("id"):
            out.add(str(p["id"]))
        elif isinstance(p, str):
            out.add(p)
    return out


def _make_baseline_client() -> TestClient:
    from switchboard.api.auth_port_adapters import configure_auth_ports
    from switchboard.api.routers.auth import store as auth_store
    from switchboard.api.routers.auth.routes import router as auth_router

    configure_auth_ports()
    auth_store.init()
    app = FastAPI(title="arch-ms77-baseline")
    app.include_router(auth_router)
    return TestClient(app)


def _make_cut_client() -> TestClient:
    from switchboard.services.auth import create_app
    from switchboard.services.auth.settings import AuthServiceSettings

    return TestClient(create_app(AuthServiceSettings(
        service_name="arch-ms77-test",
        host="127.0.0.1",
        port=8121,
    )))


import store  # noqa: E402
from switchboard.api.routers.auth import store as auth_store  # noqa: E402
from switchboard.storage.repositories import access  # noqa: E402

store.init_project_registry()
access.ensure_org("org-arch-ms77", name="ARCH-MS-77 Org", slug="arch-ms77")
store.create_project("MS77 Alpha", project_id="ms77-alpha", actor="test")
store.create_project("MS77 Beta", project_id="ms77-beta", actor="test")

baseline = _make_baseline_client()
cut = _make_cut_client()

# --- dual-app health on cut only --------------------------------------------
health = cut.get("/health")
ok(health.status_code == 200, f"cut /health status {health.status_code}")
ok(health.json().get("service") == "arch-ms77-test", "cut /health service name")

# --- parity: register → session → logout ------------------------------------
for label, client in (("baseline", baseline), ("cut", cut)):
    email = f"ms77-{label}@example.com"
    reg = client.post("/api/auth/register", json={
        "email": email,
        "display_name": f"MS77-{label}",
        "password": "password12345",
    })
    ok(reg.status_code == 200, f"{label} register status {reg.status_code}")
    ok((reg.json() or {}).get("user", {}).get("email") == email,
       f"{label} register returns email")

    sess = client.get("/api/auth/session")
    ok(sess.status_code == 200, f"{label} session after register {sess.status_code}")
    user = _session_user(sess)
    ok(user.get("email") == email, f"{label} session email")
    ok(isinstance(user.get("projects"), list), f"{label} session.projects is list")

    login = client.post("/api/auth/login", json={
        "email": email, "password": "password12345",
    })
    ok(login.status_code == 200, f"{label} login status {login.status_code}")

    bad = client.post("/api/auth/login", json={
        "email": email, "password": "wrong-password-xx",
    })
    ok(bad.status_code == 401, f"{label} bad login is 401 (got {bad.status_code})")

    logout = client.post("/api/auth/logout")
    ok(logout.status_code == 200, f"{label} logout status {logout.status_code}")
    after = client.get("/api/auth/session")
    ok(after.status_code == 401, f"{label} session after logout is 401 (got {after.status_code})")

# Side-by-side status equality for cold unauthenticated + unknown user login.
b_sess = baseline.get("/api/auth/session")
c_sess = cut.get("/api/auth/session")
ok(b_sess.status_code == c_sess.status_code == 401,
   f"unauth session parity baseline={b_sess.status_code} cut={c_sess.status_code}")

b_bad = baseline.post("/api/auth/login", json={
    "email": "nobody-ms77@example.com", "password": "nope",
})
c_bad = cut.post("/api/auth/login", json={
    "email": "nobody-ms77@example.com", "password": "nope",
})
ok(b_bad.status_code == c_bad.status_code == 401,
   f"unknown-user login parity baseline={b_bad.status_code} cut={c_bad.status_code}")

# --- grants via Access (Auth session.projects) ------------------------------
grant_email = "ms77-grants@example.com"
# Use cut app to register; grants are Access DB writes; both clients share registry.
reg = cut.post("/api/auth/register", json={
    "email": grant_email,
    "display_name": "MS77 Grants",
    "password": "password12345",
})
ok(reg.status_code == 200, f"grants register {reg.status_code}")
uid = ((reg.json() or {}).get("user") or {}).get("id")
ok(bool(uid), "grants user id present")
if uid:
    access.grant_project_role("ms77-alpha", "user", uid, "admin")

# Fresh clients after grant so cookies don't collide with prior logout state.
baseline2 = _make_baseline_client()
cut2 = _make_cut_client()
for label, client in (("baseline", baseline2), ("cut", cut2)):
    login = client.post("/api/auth/login", json={
        "email": grant_email, "password": "password12345",
    })
    ok(login.status_code == 200, f"{label} grant-user login {login.status_code}")
    sess = client.get("/api/auth/session")
    projects = _project_ids(_session_user(sess))
    ok("ms77-alpha" in projects,
       f"{label} session reflects Access grant (projects={sorted(projects)!r})")

b_projects = _project_ids(_session_user(baseline2.get("/api/auth/session")))
c_projects = _project_ids(_session_user(cut2.get("/api/auth/session")))
ok(b_projects == c_projects,
   f"grant project set parity baseline={sorted(b_projects)} cut={sorted(c_projects)}")

# --- superadmin visibility --------------------------------------------------
sa_email = "ms77-super@example.com"
reg = cut.post("/api/auth/register", json={
    "email": sa_email,
    "display_name": "MS77 Super",
    "password": "password12345",
})
sa_uid = ((reg.json() or {}).get("user") or {}).get("id")
ok(bool(sa_uid), "superadmin user id")
if sa_uid:
    auth_store.set_superadmin(sa_uid, True)

baseline3 = _make_baseline_client()
cut3 = _make_cut_client()
for label, client in (("baseline", baseline3), ("cut", cut3)):
    login = client.post("/api/auth/login", json={
        "email": sa_email, "password": "password12345",
    })
    ok(login.status_code == 200, f"{label} superadmin login {login.status_code}")
    sess = client.get("/api/auth/session")
    user = _session_user(sess)
    ok(user.get("is_superadmin") is True, f"{label} is_superadmin surfaced")
    projects = _project_ids(user)
    ok("ms77-alpha" in projects and "ms77-beta" in projects,
       f"{label} superadmin sees seeded projects (projects={sorted(projects)!r})")

ok(
    _session_user(baseline3.get("/api/auth/session")).get("is_superadmin")
    == _session_user(cut3.get("/api/auth/session")).get("is_superadmin")
    is True,
    "superadmin flag parity",
)

# --- disabled account → 403 (not 401) ---------------------------------------
dis_email = "ms77-disabled@example.com"
reg = cut.post("/api/auth/register", json={
    "email": dis_email,
    "display_name": "MS77 Disabled",
    "password": "password12345",
})
dis_uid = ((reg.json() or {}).get("user") or {}).get("id")
ok(bool(dis_uid), "disabled user id")
if dis_uid:
    with auth_store._conn() as c:  # noqa: SLF001 — test-only disable fixture
        c.execute("UPDATE users SET disabled_at=? WHERE id=?", (time.time(), dis_uid))

baseline4 = _make_baseline_client()
cut4 = _make_cut_client()
b_dis = baseline4.post("/api/auth/login", json={
    "email": dis_email, "password": "password12345",
})
c_dis = cut4.post("/api/auth/login", json={
    "email": dis_email, "password": "password12345",
})
ok(b_dis.status_code == 403, f"baseline disabled login is 403 (got {b_dis.status_code})")
ok(c_dis.status_code == 403, f"cut disabled login is 403 (got {c_dis.status_code})")
ok(b_dis.status_code == c_dis.status_code, "disabled-login status parity")

# --- production dual-strip (PM_AUTH_HTTP_PRIMARY=service) + Caddy me carve ---
app_impl_src = entrypoint_source("app")
ok("PM_AUTH_HTTP_PRIMARY" in app_impl_src,
   "monolith gates Auth HTTP mount on PM_AUTH_HTTP_PRIMARY (ARCH-MS-77)")
ok(
    '!= "service"' in app_impl_src or "!= 'service'" in app_impl_src,
    "production service primary skips dual Auth router mount",
)
ok(
    "_create_me_router" in app_impl_src or "create_me_router" in app_impl_src,
    "monolith keeps /api/auth/me thin client surface",
)
ok(
    "configure_auth_ports" in app_impl_src and "_auth_store.init" in app_impl_src,
    "monolith still binds Auth ports for middleware / me (shared package)",
)
web_unit = (ROOT / "deploy" / "projectplanner.service").read_text(encoding="utf-8")
ok("PM_AUTH_HTTP_PRIMARY=service" in web_unit,
   "live monolith unit sets PM_AUTH_HTTP_PRIMARY=service")

# Runtime: with primary=service, monolith must not expose /api/auth/login.
import subprocess
import sys

probe = subprocess.run(
    [sys.executable, "-c", r"""
import os, tempfile
from pathlib import Path
t = tempfile.mkdtemp(prefix="arch-ms77-primary-")
os.environ.update({
    "PM_DB_PATH": str(Path(t) / "m.db"),
    "PM_HELM_DB_PATH": str(Path(t) / "h.db"),
    "PM_SWITCHBOARD_DB_PATH": str(Path(t) / "s.db"),
    "PM_PROJECT_REGISTRY_DB_PATH": str(Path(t) / "p.db"),
    "PM_DYNAMIC_PROJECTS_DIR": t,
    "PM_JWT_SECRET": "arch-ms77-primary-probe",
    "PM_AUTH_MODE": "dev-open",
    "PM_AUTH_HTTP_PRIMARY": "service",
})
Path(t).mkdir(parents=True, exist_ok=True)
from fastapi.testclient import TestClient
from app import app
c = TestClient(app)
r = c.post("/api/auth/login", json={"email": "x@y.com", "password": "password12"})
# With Auth unmounted, Starlette StaticFiles catch-all often returns 405 for POST
# (or 404). Either proves there is no live Auth login surface on the monolith.
me = c.get("/api/auth/me")
# Thin me must remain mounted outside the gate.
raise SystemExit(0 if r.status_code in (404, 405) and me.status_code == 200 else
                 (r.status_code * 1000 + me.status_code))
"""],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
    timeout=120,
)
ok(probe.returncode == 0,
   "PM_AUTH_HTTP_PRIMARY=service ⇒ login 404/405 and /api/auth/me is 200"
   + (f" (rc={probe.returncode} stderr={probe.stderr[-400:]!r})" if probe.returncode else ""))

caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
ok("handle /api/auth/me*" in caddy, "Caddy carves /api/auth/me* to monolith")
ok("127.0.0.1:8121" in caddy and "handle /api/auth*" in caddy,
   "Caddy still routes /api/auth* to Auth :8121")
me_pos = caddy.find("handle /api/auth/me*")
# Broad /api/auth* handle that is not the me* carve-out.
needle = "handle /api/auth*"
auth_broad = -1
search_from = 0
while True:
    pos = caddy.find(needle, search_from)
    if pos < 0:
        break
    line_end = caddy.find("\n", pos)
    line = caddy[pos:line_end if line_end >= 0 else None]
    if "me" not in line:
        auth_broad = pos
        break
    search_from = pos + len(needle)
ok(me_pos >= 0 and auth_broad > me_pos,
   "Caddy /api/auth/me* handle is ordered before broad /api/auth*")
ok("ARCH-MS-77" in caddy, "Caddyfile documents ARCH-MS-77 dual-strip")

# --- optional live smoke (edge only; no register on prod) -------------------
if os.environ.get("ARCH_MS77_LIVE_SMOKE", "").strip() in ("1", "true", "yes"):
    base = (os.environ.get("ARCH_MS77_BASE_URL") or "https://plan.taikunai.com").rstrip("/")

    def _http(method: str, path: str, body: bytes | None = None) -> tuple[int, str]:
        req = urllib.request.Request(
            base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    code, _ = _http("GET", "/api/auth/session")
    ok(code == 401, f"live session unauth is 401 (got {code})")
    code, _ = _http(
        "POST",
        "/api/auth/login",
        b'{"email":"ms77-live-smoke@example.com","password":"wrong-password-xx"}',
    )
    ok(code == 401, f"live bad login is 401 (got {code})")
else:
    ok(True, "live smoke skipped (set ARCH_MS77_LIVE_SMOKE=1 to enable)")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
