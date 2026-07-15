#!/usr/bin/env python3
"""ARCH-MS-97: Deliverables/mission ADR charter + Mode A thin surface lock."""
from __future__ import annotations

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


adr = ROOT / "docs/decisions/0014-deliverables-mission-process-strangler.md"
surface = ROOT / "docs/deliverables/thin_day_one_surface.md"
tracker = ROOT / "docs/ARCH-MS-EXECUTION.md"

ok(adr.is_file(), "ADR-0014 present")
adr_text = adr.read_text(encoding="utf-8") if adr.is_file() else ""
ok("ADR-0014" in adr_text or "0014" in adr_text, "ADR titled 0014")
ok("ARCH-MS-97" in adr_text, "ADR cites ARCH-MS-97")
ok("arch-ms-deliverables-service" in adr_text,
   "ADR is plan-of-record for arch-ms-deliverables-service")
ok("8124" in adr_text, "ADR locks port :8124")
ok("Mode A" in adr_text or "thin day-one" in adr_text.lower(), "ADR Mode A / thin surface")
ok("yellow" in adr_text.lower() or "CONDITIONAL" in adr_text, "ADR yellow-light / conditional cut")
ok("No-Go" in adr_text, "ADR documents No-Go exit")
ok("ADR-0013" in adr_text and "ADR-0012" in adr_text, "ADR reuses Coord/Tasks lineage")
ok("network coupling" in adr_text.lower() or "Never convert" in adr_text,
   "ADR forbids network-wrap without independence")
ok("MCP" in adr_text and "8111" in adr_text, "ADR keeps MCP on monolith/:8111")
ok("closure" in adr_text.lower() and "mission" in adr_text.lower(),
   "ADR scopes mission/closure read surface")

ok(surface.is_file(), "thin_day_one_surface.md present")
surf = surface.read_text(encoding="utf-8") if surface.is_file() else ""
ok(":8124" in surf or "8124" in surf, "thin surface names :8124")
for route in (
    "/api/deliverables",
    "/api/mission_status",
    "/closure_report",
    "/dependency_graph",
    "/breakdown_proposals",
):
    ok(route in surf, f"thin surface lists {route}")
ok("closure_verify" in surf and "stay" in surf.lower(),
   "thin surface keeps closure write off day-one")
ok("coordinator_tick" in surf, "thin surface keeps coordinator tick off day-one")
ok("PM_DELIVERABLES_HTTP_PRIMARY" in surf or "dual-strip" in surf.lower(),
   "thin surface names dual-strip analogue")

ok(not (ROOT / "deploy/switchboard-deliverables.service").is_file(),
   "no production Deliverables systemd unit yet (charter only)")
caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
live = "\n".join(
    line for line in caddy.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
ok("8124" not in live, "live Caddy does not yet route Deliverables :8124")

tracker_text = tracker.read_text(encoding="utf-8") if tracker.is_file() else ""
ok("ARCH-MS-97" in tracker_text, "execution tracker lists ARCH-MS-97")
ok("0014" in tracker_text or "ADR-0014" in tracker_text, "tracker links ADR-0014")
ok("arch-ms-deliverables-service" in tracker_text,
   "tracker lists arch-ms-deliverables-service")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
