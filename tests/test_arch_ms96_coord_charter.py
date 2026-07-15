#!/usr/bin/env python3
"""ARCH-MS-96: Coord/board ADR charter + Mode A thin surface lock."""
from __future__ import annotations

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


adr = ROOT / "docs/decisions/0013-coord-board-process-strangler.md"
surface = ROOT / "docs/coord/thin_day_one_surface.md"
tracker = ROOT / "docs/ARCH-MS-EXECUTION.md"

ok(adr.is_file(), "ADR-0013 present")
adr_text = adr.read_text(encoding="utf-8") if adr.is_file() else ""
ok("ADR-0013" in adr_text or "0013" in adr_text, "ADR titled 0013")
ok("ARCH-MS-96" in adr_text, "ADR cites ARCH-MS-96")
ok("arch-ms-coord-service" in adr_text, "ADR is plan-of-record for arch-ms-coord-service")
ok("8123" in adr_text, "ADR locks port :8123")
ok("Mode A" in adr_text or "thin day-one" in adr_text.lower(), "ADR Mode A / thin surface")
ok("yellow" in adr_text.lower() or "CONDITIONAL" in adr_text, "ADR yellow-light / conditional cut")
ok("No-Go" in adr_text or "No-Go" in adr_text.replace("–", "-"), "ADR documents No-Go exit")
ok("ADR-0011" in adr_text and "ADR-0012" in adr_text, "ADR reuses Auth/Tasks playbook lineage")
ok("Never convert" in adr_text or "network coupling" in adr_text.lower(),
   "ADR forbids network-wrap without independence")
ok("MCP" in adr_text and "8111" in adr_text, "ADR keeps MCP on monolith/:8111")

ok(surface.is_file(), "thin_day_one_surface.md present")
surf = surface.read_text(encoding="utf-8") if surface.is_file() else ""
ok(":8123" in surf or "8123" in surf, "thin surface names :8123")
for route in (
    "/api/board",
    "/api/signals",
    "/ixp/v1/delta",
    "/api/coordination",
    "/api/coordinator_decisions",
):
    ok(route in surf, f"thin surface lists {route}")
ok("/api/people" in surf or "people" in surf.lower(), "thin surface excludes people (noted)")
ok("coordinator_dispatch" in surf, "thin surface keeps write dispatch off day-one")
ok("PM_COORD_HTTP_PRIMARY" in surf or "dual-strip" in surf.lower(),
   "thin surface names dual-strip analogue")

# Must not invent a live Coord cut yet
ok(not (ROOT / "deploy/switchboard-coord.service").is_file(),
   "no production Coord systemd unit yet (charter only)")
caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
live = "\n".join(
    line for line in caddy.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
ok("8123" not in live, "live Caddy does not yet route Coord :8123")

tracker_text = tracker.read_text(encoding="utf-8") if tracker.is_file() else ""
ok("ARCH-MS-96" in tracker_text, "execution tracker lists ARCH-MS-96")
ok("0013" in tracker_text or "ADR-0013" in tracker_text, "tracker links ADR-0013")
ok("arch-ms-coord-service" in tracker_text, "tracker lists arch-ms-coord-service")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
