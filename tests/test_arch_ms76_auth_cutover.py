#!/usr/bin/env python3
"""ARCH-MS-76: live Caddy + systemd Auth cutover (Go path)."""
from __future__ import annotations

import re

from path_setup import ROOT, entrypoint_source

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
ok("handle /api/auth*" in caddy, "production Caddyfile has /api/auth* handle")
ok("127.0.0.1:8121" in caddy, "production Caddyfile proxies Auth to :8121")
# Auth handle must appear before the catch-all handle inside plan.taikunai.com.
plan_block = caddy.split("plan.taikunai.com", 1)[-1].split("format.taikunai.com", 1)[0]
auth_pos = plan_block.find("handle /api/auth*")
catch_pos = plan_block.find("handle {")
ok(auth_pos >= 0 and catch_pos > auth_pos,
   "Auth handle is ordered before catch-all :8110 handle")
ok("health_uri /health" in plan_block[auth_pos:catch_pos],
   "Auth upstream uses /health active probe")
ok("rollback" in caddy.lower() or "ARCH-MS-76" in caddy,
   "Caddyfile documents Auth cutover / rollback")

unit = ROOT / "deploy" / "switchboard-auth.service"
ok(unit.is_file(), "live deploy/switchboard-auth.service exists")
unit_text = unit.read_text(encoding="utf-8") if unit.is_file() else ""
ok("switchboard.services.auth.app:create_app" in unit_text,
   "live systemd unit uses Auth factory app")
ok("--port 8121" in unit_text or "SWITCHBOARD_AUTH_PORT=8121" in unit_text,
   "live systemd unit binds :8121")
ok("WantedBy=multi-user.target" in unit_text, "live unit is installable")

example = ROOT / "deploy" / "auth" / "switchboard-auth.service.example"
ok(example.is_file(), "deploy/auth example unit still present")

readme = (ROOT / "deploy" / "auth" / "README.md").read_text(encoding="utf-8")
ok("ARCH-MS-76" in readme, "auth README references ARCH-MS-76 cutover")
ok("8121" in readme, "auth README documents :8121")

provision = (ROOT / "deploy" / "PROVISION.md").read_text(encoding="utf-8")
ok("switchboard-auth" in provision, "PROVISION enables switchboard-auth")
ok("ARCH-MS-76" in provision, "PROVISION has Auth cutover checklist")
ok("rollback" in provision.lower(), "PROVISION documents Auth rollback")

redeploy = (ROOT / "deploy" / "redeploy.sh").read_text(encoding="utf-8")
ok("switchboard-auth" in redeploy, "redeploy.sh manages switchboard-auth")
ok(re.search(r"8121/health", redeploy) is not None,
   "redeploy proves Auth /health before Caddy reload")
# Auth restart must precede Caddy sync.
restart_pos = redeploy.find('section "restart services"')
caddy_pos = redeploy.find('section "Caddyfile"')
ok(restart_pos >= 0 and caddy_pos > restart_pos,
   "redeploy starts Auth before Caddy reload")

runbook = (ROOT / "docs" / "runbooks" / "auth-caddy-cutover-rollback.md").read_text(
    encoding="utf-8"
)
ok("ARCH-MS-76" in runbook, "runbook covers live ARCH-MS-76 cutover")
ok("reload" in runbook.lower(), "runbook uses caddy reload for rollback")
ok("8110" in runbook and "8121" in runbook, "runbook names both ports")

gate = (ROOT / "docs" / "AUTH-INDEPENDENCE-GATE.md").read_text(encoding="utf-8")
ok("ARCH-MS-76" in gate or "ARCH-MS-77" in gate, "independence gate mentions Auth cut")

# ARCH-MS-77: production dual-strip via PM_AUTH_HTTP_PRIMARY=service (+ Caddy me carve-out).
app_impl_src = entrypoint_source("app")
ok("PM_AUTH_HTTP_PRIMARY" in app_impl_src,
   "monolith gates Auth HTTP mount on PM_AUTH_HTTP_PRIMARY (ARCH-MS-77)")
ok(
    "_create_me_router" in app_impl_src or "create_me_router" in app_impl_src,
    "monolith keeps /api/auth/me thin surface",
)
web_unit = (ROOT / "deploy" / "projectplanner.service").read_text(encoding="utf-8")
ok("PM_AUTH_HTTP_PRIMARY=service" in web_unit,
   "live monolith unit sets PM_AUTH_HTTP_PRIMARY=service")
ok("handle /api/auth/me" in caddy or "handle /api/auth/me*" in caddy,
   "Caddy carves /api/auth/me* to monolith")
ok("ARCH-MS-77" in caddy, "Caddyfile documents ARCH-MS-77 dual-strip")

frag = ROOT / "deploy" / "skeleton" / "Caddyfile.auth-fragment.example"
ok(frag.is_file(), "Caddyfile.auth-fragment.example retained as drill reference")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
