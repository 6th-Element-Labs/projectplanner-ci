#!/usr/bin/env python3
"""ARCH-MS-111: Deliverables production cutover and rollback gates."""
from __future__ import annotations

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
    passed += int(condition)
    failed += int(not condition)


caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
matcher = re.search(
    r"@deliverables_day_one_reads\s*\{(?P<body>.*?)\n\s*\}\s*"
    r"handle @deliverables_day_one_reads\s*\{(?P<handle>.*?)\n\s*\}",
    caddy, re.DOTALL,
)
ok(matcher is not None, "Caddy has a bounded Deliverables matcher and handle")
body = matcher.group("body") if matcher else ""
handle = matcher.group("handle") if matcher else ""
ok("method GET" in body, "Deliverables cut is GET-only")
ok("path_regexp deliverables_day_one ^/api/" in body and body.rstrip().endswith("$"),
   "Deliverables path matcher is anchored")
ok("reverse_proxy 127.0.0.1:8124" in handle, "Deliverables reads proxy to :8124")
ok(re.search(r"reverse_proxy 127\.0\.0\.1:8124.*?health_uri /ready", caddy, re.DOTALL)
   is not None, "Caddy actively probes dependency readiness")
ok(matcher is not None and matcher.start() < caddy.find("    handle {\n"),
   "Deliverables handle precedes monolith catch-all")
ok("handle /api/deliverables*" not in caddy, "no broad Deliverables wildcard cut exists")

unit = (ROOT / "deploy" / "switchboard-deliverables.service").read_text(encoding="utf-8")
for needle in (
    "User=projectplanner", "Group=projectplanner", "ProtectSystem=strict",
    "NoNewPrivileges=yes", "ReadWritePaths=/var/lib/projectplanner",
    "MemoryMax=250M", "switchboard.services.deliverables.app:create_app",
    "8124", "WantedBy=multi-user.target",
):
    ok(needle in unit, f"production Deliverables unit includes {needle}")

mono = (ROOT / "deploy" / "projectplanner.service").read_text(encoding="utf-8")
ok("PM_DELIVERABLES_HTTP_PRIMARY=service" in mono,
   "production monolith enables Deliverables dual-strip")

os.environ.setdefault("PM_AUTH_MODE", "dev-open")
from switchboard.api import deps  # noqa: E402
from switchboard.api.routers import deliverables  # noqa: E402

router = deliverables.create_router(
    resolve_project=deps.resolve_project,
    resolve_principal=deps.resolve_principal,
    etag_json=deps.etag_json,
    sibling_bc_only=True,
)
methods_by_path = {
    (route.path, method)
    for route in router.routes
    for method in (route.methods or set())
}
cut_reads = (
    "/api/deliverables", "/api/deliverables/{deliverable_id}",
    "/api/mission_status", "/api/deliverables/{deliverable_id}/mission_status",
    "/api/deliverables/{deliverable_id}/closure_report",
    "/api/deliverables/{deliverable_id}/dependency_graph",
    "/api/deliverables/breakdown_proposals",
    "/api/deliverables/breakdown_proposals/{proposal_id}",
)
for path in cut_reads:
    ok((path, "GET") not in methods_by_path, f"dual-strip omits GET {path}")
for path, method in (
    ("/api/deliverables", "POST"),
    ("/api/deliverables/{deliverable_id}/milestones", "POST"),
    ("/api/deliverables/{deliverable_id}/closure_request", "POST"),
):
    ok((path, method) in methods_by_path, f"dual-strip preserves {method} {path}")

redeploy = (ROOT / "deploy" / "redeploy.sh").read_text(encoding="utf-8")
inventory = (ROOT / "deploy" / "service-cut-inventory.json").read_text(encoding="utf-8")
ok('"switchboard-deliverables"' in inventory and '"ready": "/ready"' in inventory,
   "declarative inventory registers Deliverables health/readiness")
ok("service_cut_inventory.py" in redeploy and "REQUIRED_READY_URLS" in redeploy,
   "redeploy derives cut gates from the declarative inventory")
for needle, message in (
    ("DELIVERABLES_WAS_ACTIVE", "rollback snapshots active state"),
    ("DELIVERABLES_WAS_ENABLED", "rollback snapshots enabled state"),
    ("switchboard-deliverables.service.present", "rollback snapshots prior unit"),
):
    ok(needle in redeploy, message)
ok('"port": 8124' in inventory, "runtime proof includes Deliverables from inventory")
ok('"@deliverables_day_one_reads"' in inventory,
   "runtime proof verifies exact edge owner from inventory")
ok('"health": "/health"' in inventory and '"ready": "/ready"' in inventory,
   "Deliverables health and readiness gate Caddy")
restart = redeploy.find('section "restart services"')
sync = redeploy.find("sync_caddy_fail_closed.sh")
proof = redeploy.find("verify_runtime_deploy.py")
ok(0 <= restart < sync < proof, "restart and all-health gate precede edge proof")
dual_strip_restart = redeploy.find('section "Deliverables dual-strip monolith"')
ok("DELIVERABLES_CUT_WAS_LIVE" in redeploy,
   "redeploy detects first activation versus an already-live cut")
ok(0 <= sync < dual_strip_restart < proof,
   "first activation moves healthy edge before restarting stripped monolith")
ok("PRE_DELIVERABLES_CUT_SERVICES" in redeploy,
   "first activation leaves old monolith serving until edge moves")
for prior in ("switchboard-auth", "switchboard-tasks", "switchboard-coord"):
    ok(f'"{prior}"' in inventory, f"inventory retains prior cut {prior}")

# Dead :8124 must leave the live edge untouched.
with tempfile.TemporaryDirectory(prefix="arch-ms111-dead-deliverables-") as tmp:
    base = Path(tmp)
    root = base / "repo"
    live = base / "live" / "Caddyfile"
    fake = base / "bin"
    (root / "deploy").mkdir(parents=True)
    live.parent.mkdir(parents=True)
    fake.mkdir()
    for name in ("sync_caddy_fail_closed.sh", "wait-for-health.sh", "Caddyfile"):
        shutil.copy2(ROOT / "deploy" / name, root / "deploy" / name)
    prior = "# prior edge remains live\n"
    live.write_text(prior, encoding="utf-8")
    for name, script in {
        "curl": "#!/bin/sh\nprintf 000\nexit 1\n",
        "sudo": "#!/bin/sh\nexec \"$@\"\n",
        "caddy": "#!/bin/sh\nexit 0\n",
        "systemctl": "#!/bin/sh\nexit 0\n",
    }.items():
        path = fake / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{fake}:{env['PATH']}", "PLAN_ROOT": str(root),
                "CADDY_LIVE": str(live), "HEALTH_TIMEOUT_SECONDS": "1",
                "HEALTH_INTERVAL_SECONDS": "1", "HEALTH_CURL_TIMEOUT_SECONDS": "1"})
    result = subprocess.run(
        ["bash", str(root / "deploy" / "sync_caddy_fail_closed.sh"),
         "http://127.0.0.1:8124/health"],
        env=env, text=True, capture_output=True, timeout=15, check=False,
    )
    ok(result.returncode != 0, "dead Deliverables backend blocks deployment")
    ok(live.read_text(encoding="utf-8") == prior,
       "dead Deliverables backend preserves prior edge")

# Static stale-config proof must fail when the new exact owner is absent.
with tempfile.TemporaryDirectory(prefix="arch-ms111-stale-caddy-") as tmp:
    stale = Path(tmp) / "Caddyfile"
    stale.write_text("# stale live config\n", encoding="utf-8")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result = subprocess.run([
        "python3", str(ROOT / "scripts" / "verify_runtime_deploy.py"),
        "--root", str(ROOT), "--canonical-sha", head,
        "--caddy-live", str(stale), "--skip-live-probes",
        "--edge-owns", "@deliverables_day_one_reads:8124",
    ], cwd=ROOT, text=True, capture_output=True, check=False)
    ok(result.returncode != 0, "stale live Caddy config fails Deliverables proof")

runbook = (ROOT / "docs" / "runbooks" / "deliverables-caddy-cutover-rollback.md")
text = runbook.read_text(encoding="utf-8")
ok("Never stop" in text, "runbook forbids stop-first cutover")
ok(text.find("Restore the prior monolith") < text.find("Restore and reload the prior Caddyfile"),
   "runbook restores monolith before edge")

print(f"\nARCH-MS-111 Deliverables cutover: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
