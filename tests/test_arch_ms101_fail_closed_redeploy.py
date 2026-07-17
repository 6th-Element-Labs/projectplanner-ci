#!/usr/bin/env python3
"""ARCH-MS-101: fail-closed redeploy + reusable runtime-proof harness."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


REDEPLOY = ROOT / "deploy" / "redeploy.sh"
SYNC = ROOT / "deploy" / "sync_caddy_fail_closed.sh"
VERIFY = ROOT / "scripts" / "verify_runtime_deploy.py"
WAIT = ROOT / "deploy" / "wait-for-health.sh"

redeploy = REDEPLOY.read_text(encoding="utf-8")
sync = SYNC.read_text(encoding="utf-8")
inventory = (ROOT / "deploy" / "service-cut-inventory.json").read_text(encoding="utf-8")

# --- redeploy wires Tasks as a required routed service -----------------------
ok(REDEPLOY.is_file() and SYNC.is_file() and VERIFY.is_file(),
   "redeploy, fail-closed Caddy sync, and runtime proof scripts exist")
ok("switchboard-tasks" in redeploy, "redeploy.sh manages switchboard-tasks")
ok('"switchboard-tasks"' in inventory,
   "switchboard-tasks is in APP_SERVICES (required deployed service)")
ok('"switchboard-tasks"' in inventory and '"switchboard-auth"' in inventory
   and 'systemctl enable "${CUT_SERVICES[@]}"' in redeploy,
   "redeploy enables switchboard-tasks (and Auth)")
ok('"port": 8122' in inventory and '"health": "/health"' in inventory,
   "redeploy requires :8122 health before Caddy")
ok("sync_caddy_fail_closed.sh" in redeploy,
   "redeploy delegates Caddy sync to the fail-closed helper")
ok("verify_runtime_deploy.py" in redeploy,
   "redeploy runs the reusable runtime-proof harness")

# Ordering: restart services → fail-closed Caddy (health inside helper) → runtime proof
restart_pos = redeploy.find('section "restart services"')
caddy_pos = redeploy.find("sync_caddy_fail_closed.sh")
proof_pos = redeploy.find("verify_runtime_deploy.py")
ok(restart_pos >= 0 and caddy_pos > restart_pos,
   "Tasks/Auth restart precedes fail-closed Caddy sync")
ok(proof_pos > caddy_pos > 0, "runtime proof runs after Caddy sync attempt")

# --- Auth regression: Auth remains a first-class required service ------------
ok("switchboard-auth" in redeploy, "Auth still managed by redeploy (no regression)")
ok('"port": 8121' in inventory and '"health": "/health"' in inventory,
   "Auth :8121 health still required before Caddy")
ok('"switchboard-auth"' in inventory,
   "switchboard-auth remains in APP_SERVICES")

# --- fail-closed helper leaves live Caddy untouched on health failure --------
ok("leaving live" in sync and "untouched" in sync,
   "fail-closed helper documents preserving the prior live Caddyfile")
ok("wait-for-health.sh" in sync, "fail-closed helper reuses the bounded health gate")
ok("caddy validate" in sync, "fail-closed helper validates before overwrite")

with tempfile.TemporaryDirectory(prefix="arch-ms101-caddy-") as tmp:
    base = Path(tmp)
    fake_bin = base / "bin"
    live = base / "live" / "Caddyfile"
    repo = base / "repo" / "deploy" / "Caddyfile"
    root = base / "repo"
    fake_bin.mkdir()
    live.parent.mkdir(parents=True)
    repo.parent.mkdir(parents=True)
    live.write_text("# LIVE prior edge\nhandle /api/tasks* {\n    reverse_proxy 127.0.0.1:8110\n}\n",
                    encoding="utf-8")
    prior = live.read_text(encoding="utf-8")
    repo.write_text("# REPO new edge\nhandle /api/tasks* {\n    reverse_proxy 127.0.0.1:8122\n}\n",
                    encoding="utf-8")
    # Point helper's wait-for-health at the real one; stub caddy/systemctl/sudo/curl.
    (root / "deploy").mkdir(exist_ok=True)
    shutil.copy2(WAIT, root / "deploy" / "wait-for-health.sh")
    shutil.copy2(SYNC, root / "deploy" / "sync_caddy_fail_closed.sh")

    executable(
        fake_bin / "curl",
        """#!/bin/sh
# Always unhealthy — simulates dead unit / no listener.
echo 000
exit 1
""",
    )
    executable(
        fake_bin / "caddy",
        """#!/bin/sh
printf 'caddy %s\\n' "$*" >> "$ARCH101_CALLS"
exit 0
""",
    )
    executable(
        fake_bin / "systemctl",
        """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$ARCH101_CALLS"
exit 0
""",
    )
    executable(
        fake_bin / "sudo",
        """#!/bin/sh
printf 'sudo %s\\n' "$*" >> "$ARCH101_CALLS"
exec "$@"
""",
    )
    calls = base / "calls.log"
    env = os.environ.copy()
    env.update({
        "PATH": f"{fake_bin}:{env['PATH']}",
        "PLAN_ROOT": str(root),
        "CADDY_LIVE": str(live),
        "REPO_CADDY": str(repo),
        "HEALTH_TIMEOUT_SECONDS": "1",
        "HEALTH_INTERVAL_SECONDS": "1",
        "HEALTH_CURL_TIMEOUT_SECONDS": "1",
        "ARCH101_CALLS": str(calls),
    })
    dead = subprocess.run(
        ["bash", str(root / "deploy" / "sync_caddy_fail_closed.sh"),
         "http://127.0.0.1:9/health"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    after = live.read_text(encoding="utf-8")
    call_log = calls.read_text(encoding="utf-8") if calls.exists() else ""
    ok(dead.returncode != 0, "dead-unit health failure exits non-zero")
    ok(after == prior, "failed Tasks health preserves the prior live Caddyfile")
    ok("leaving live" in dead.stderr and "untouched" in dead.stderr,
       "failure message names the preserved live Caddyfile")
    ok("sudo cp" not in call_log and "systemctl reload" not in call_log,
       "dead-unit path never copies or reloads Caddy")

    # Healthy path: curl returns 200 → live Caddyfile is replaced.
    executable(
        fake_bin / "curl",
        """#!/bin/sh
# Emulate curl -w '%{http_code}' used by wait-for-health.sh
printf '200'
exit 0
""",
    )
    if calls.exists():
        calls.write_text("", encoding="utf-8")
    live.write_text(prior, encoding="utf-8")
    healthy = subprocess.run(
        ["bash", str(root / "deploy" / "sync_caddy_fail_closed.sh"),
         "http://127.0.0.1:9/health"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    ok(healthy.returncode == 0, "healthy pre-Caddy probes allow Caddy sync")
    ok("8122" in live.read_text(encoding="utf-8"),
       "healthy path installs the repo Caddyfile to the live path")

# --- runtime proof harness: schema + static edge ownership -------------------
verify_src = VERIFY.read_text(encoding="utf-8")
ok("switchboard.runtime_deploy.v1" in verify_src,
   "runtime proof emits switchboard.runtime_deploy.v1")
ok("exact_sha" in verify_src and "caddy_checksum" in verify_src,
   "runtime proof compares canonical/VM SHA and Caddy checksums")
ok("unit:" in verify_src and "listener:" in verify_src and "health:" in verify_src,
   "runtime proof checks unit state, listener, and local health")
ok("edge:" in verify_src or "edge_owns" in verify_src,
   "runtime proof checks edge ownership")

static = subprocess.run(
    [
        "python3",
        str(VERIFY),
        "--root",
        str(ROOT),
        "--canonical-sha",
        subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "--skip-live-probes",
        "--edge-owns",
        "/api/auth*:8121",
        "--edge-owns",
        "/api/tasks*:8122",
    ],
    cwd=ROOT,
    text=True,
    capture_output=True,
    timeout=30,
    check=False,
)
ok(static.returncode == 0, "static runtime proof passes for Auth+Tasks edge ownership")
try:
    evidence = json.loads(static.stdout)
except json.JSONDecodeError:
    evidence = {}
ok(evidence.get("schema") == "switchboard.runtime_deploy.v1",
   "static proof JSON uses runtime_deploy schema")
ok(evidence.get("ok") is True, "static proof reports ok=true for live Caddyfile intent")
check_names = {c.get("name") for c in evidence.get("checks", [])}
ok(any(n.startswith("edge:/api/auth*") for n in check_names),
   "static proof includes Auth edge ownership check")
ok(any(n.startswith("edge:/api/tasks*") for n in check_names),
   "static proof includes Tasks edge ownership check")

# --- PROVISION / runbook mention Tasks enable-before-Caddy -------------------
provision = (ROOT / "deploy" / "PROVISION.md").read_text(encoding="utf-8")
runbook = (ROOT / "docs" / "runbooks" / "tasks-caddy-cutover-rollback.md").read_text(
    encoding="utf-8"
)
ok("switchboard-tasks" in provision, "PROVISION documents switchboard-tasks")
ok("8122" in provision, "PROVISION documents Tasks :8122 health")
ok("ARCH-MS-101" in runbook or "redeploy.sh" in runbook,
   "Tasks runbook references redeploy / ARCH-MS-101 fail-closed path")

print(f"\nARCH-MS-101 fail-closed redeploy: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
