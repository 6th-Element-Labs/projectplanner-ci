#!/usr/bin/env python3
"""ARCH-MS-106: Coord production cutover, dual-strip, and deploy gates."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
routes = (
    "/api/board", "/api/signals", "/ixp/v1/delta",
    "/api/coordination", "/api/coordinator_decisions",
)
catchall = caddy.find("    handle {\n")
for route in routes:
    pattern = rf"handle {re.escape(route)}\s*\{{\s*reverse_proxy 127\.0\.0\.1:8123"
    match = re.search(pattern, caddy, re.DOTALL)
    ok(match is not None, f"exact Caddy handle owns {route} on :8123")
    ok(match is not None and match.start() < catchall,
       f"{route} handle precedes the monolith catch-all")
    ok(f"handle {route}*" not in caddy, f"{route} ownership is not widened by wildcard")

unit = (ROOT / "deploy" / "switchboard-coord.service").read_text(encoding="utf-8")
for needle in (
    "User=projectplanner", "Group=projectplanner", "ProtectSystem=strict",
    "NoNewPrivileges=yes", "ReadWritePaths=/var/lib/projectplanner",
    "switchboard.services.coord.app:create_app", "8123",
    "WantedBy=multi-user.target",
):
    ok(needle in unit, f"production Coord unit includes {needle}")

mono = (ROOT / "deploy" / "projectplanner.service").read_text(encoding="utf-8")
ok("PM_COORD_HTTP_PRIMARY=service" in mono, "production monolith enables Coord dual-strip")

# Prove the selective router switches remove only the five cut-owned routes.
os.environ.setdefault("PM_AUTH_MODE", "dev-open")
from switchboard.api import deps  # noqa: E402
from switchboard.api.routers import board, coordination, monitors  # noqa: E402

stripped = FastAPI()
board_router = board.create_router(
    resolve_project=deps.resolve_project,
    etag_json=deps.etag_json,
    saturation_snapshot=lambda project: {"project": project},
    sibling_bc_only=True,
)
monitors_router = monitors.create_router(
    resolve_project=deps.resolve_project,
    resolve_principal=deps.resolve_principal,
    resolve_body_project=deps.resolve_body_project,
    omit_coord_delta=True,
)
coordination_router = coordination.create_router(
    resolve_project=deps.resolve_project,
    resolve_principal=deps.resolve_principal,
    sibling_bc_only=True,
)
for router in (board_router, monitors_router, coordination_router):
    stripped.include_router(router)
paths = {
    route.path
    for router in (board_router, monitors_router, coordination_router)
    for route in router.routes
}
for route in routes:
    ok(route not in paths, f"dual-strip monolith omits {route}")
for sibling in (
    "/api/people", "/api/dispatch/status", "/ixp/v1/saturation_signals",
    "/ixp/v1/working_agreement",
):
    ok(sibling in paths, f"dual-strip preserves sibling {sibling}")

redeploy = (ROOT / "deploy" / "redeploy.sh").read_text(encoding="utf-8")
inventory = (ROOT / "deploy" / "service-cut-inventory.json").read_text(encoding="utf-8")
restart = redeploy.find('section "restart services"')
sync = redeploy.find("sync_caddy_fail_closed.sh")
proof = redeploy.find("verify_runtime_deploy.py")
ok('"switchboard-coord"' in inventory,
   "Coord is a required deployed service")
ok(all(f'"{name}"' in inventory for name in
       ("switchboard-auth", "switchboard-tasks", "switchboard-coord"))
   and 'systemctl enable "${CUT_SERVICES[@]}"' in redeploy,
   "Coord is boot-enabled with prior cuts")
ok('"port": 8123' in inventory and '"health": "/health"' in inventory,
   "Coord health is required before Caddy")
ok(0 <= restart < sync < proof, "restart and health-gated Caddy precede runtime proof")
ok("COORD_WAS_ACTIVE" in redeploy and "COORD_WAS_ENABLED" in redeploy,
   "rollback snapshots prior Coord lifecycle")
ok("switchboard-coord.service.present" in redeploy,
   "rollback snapshots/restores the prior Coord unit")
ok("restore_tasks_cut_topology" in redeploy,
   "runtime failure arms the complete process-cut rollback")
for prior in ("switchboard-auth:8121", "switchboard-tasks:8122"):
    name, port = prior.split(":")
    ok(f'"{name}"' in inventory and f'"port": {port}' in inventory,
       f"runtime proof retains prior cut {prior}")

# A dead :8123 must leave the old edge byte-for-byte untouched.
with tempfile.TemporaryDirectory(prefix="arch-ms106-dead-coord-") as tmp:
    base = Path(tmp)
    root = base / "repo"
    live = base / "live" / "Caddyfile"
    fake = base / "bin"
    (root / "deploy").mkdir(parents=True)
    live.parent.mkdir(parents=True)
    fake.mkdir()
    shutil.copy2(ROOT / "deploy" / "sync_caddy_fail_closed.sh", root / "deploy")
    shutil.copy2(ROOT / "deploy" / "wait-for-health.sh", root / "deploy")
    shutil.copy2(ROOT / "deploy" / "Caddyfile", root / "deploy")
    prior = "# prior edge still owns Coord on monolith\n"
    live.write_text(prior, encoding="utf-8")
    for name, body in {
        "curl": "#!/bin/sh\nprintf 000\nexit 1\n",
        "sudo": "#!/bin/sh\nexec \"$@\"\n",
        "caddy": "#!/bin/sh\nexit 0\n",
        "systemctl": "#!/bin/sh\nexit 0\n",
    }.items():
        path = fake / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake}:{env['PATH']}", "PLAN_ROOT": str(root),
        "CADDY_LIVE": str(live), "HEALTH_TIMEOUT_SECONDS": "1",
        "HEALTH_INTERVAL_SECONDS": "1", "HEALTH_CURL_TIMEOUT_SECONDS": "1",
    })
    dead = subprocess.run(
        ["bash", str(root / "deploy" / "sync_caddy_fail_closed.sh"),
         "http://127.0.0.1:8123/health"],
        env=env, text=True, capture_output=True, timeout=15, check=False,
    )
    ok(dead.returncode != 0, "dead Coord backend blocks deployment")
    ok(live.read_text(encoding="utf-8") == prior,
       "dead Coord backend preserves the prior edge")

# A stale live Caddyfile must fail the checksum gate even when repo intent is good.
with tempfile.TemporaryDirectory(prefix="arch-ms106-stale-caddy-") as tmp:
    stale = Path(tmp) / "Caddyfile"
    stale.write_text("# stale live config\n", encoding="utf-8")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result = subprocess.run([
        "python3", str(ROOT / "scripts" / "verify_runtime_deploy.py"),
        "--root", str(ROOT), "--canonical-sha", head,
        "--caddy-live", str(stale), "--skip-live-probes",
        "--edge-owns", "/api/auth*:8121", "--edge-owns", "/api/tasks*:8122",
        *sum((["--edge-owns", f"{route}:8123"] for route in routes), []),
    ], cwd=ROOT, text=True, capture_output=True, check=False)
    ok(result.returncode != 0, "stale live Caddy checksum fails deployment proof")

print(f"\nARCH-MS-106 Coord cutover: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
