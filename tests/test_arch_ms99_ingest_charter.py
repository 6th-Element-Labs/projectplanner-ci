#!/usr/bin/env python3
"""ARCH-MS-99: Ingest/inbox ADR charter + Mode A thin surface lock."""
from __future__ import annotations

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


adr = ROOT / "docs/decisions/0016-ingest-inbox-process-strangler.md"
surface = ROOT / "docs/ingest/thin_day_one_surface.md"
tracker = ROOT / "docs/ARCH-MS-EXECUTION.md"

ok(adr.is_file(), "ADR-0016 present")
adr_text = adr.read_text(encoding="utf-8") if adr.is_file() else ""
ok("ADR-0016" in adr_text or "0016" in adr_text, "ADR titled 0016")
ok("ARCH-MS-99" in adr_text, "ADR cites ARCH-MS-99")
ok("arch-ms-ingest-service" in adr_text,
   "ADR is plan-of-record for arch-ms-ingest-service")
ok("8126" in adr_text, "ADR locks port :8126")
ok("Mode A" in adr_text or "thin day-one" in adr_text.lower(), "ADR Mode A / thin surface")
ok("yellow" in adr_text.lower() or "CONDITIONAL" in adr_text, "ADR yellow-light / conditional cut")
ok("No-Go" in adr_text, "ADR documents No-Go exit")
ok("ADR-0015" in adr_text and "ADR-0012" in adr_text, "ADR reuses Tally/Tasks lineage")
ok("network coupling" in adr_text.lower() or "Never convert" in adr_text,
   "ADR forbids network-wrap without independence")
ok("MCP" in adr_text and "8111" in adr_text, "ADR keeps MCP on monolith/:8111")
ok("intake" in adr_text.lower() and "inbox" in adr_text.lower(),
   "ADR scopes intake / inbox surface")

ok(surface.is_file(), "thin_day_one_surface.md present")
surf = surface.read_text(encoding="utf-8") if surface.is_file() else ""
ok(":8126" in surf or "8126" in surf, "thin surface names :8126")
for route in (
    "/api/inbox",
    "/api/intake",
):
    ok(route in surf, f"thin surface lists {route}")
ok("intake/upload" in surf and "stay" in surf.lower(),
   "thin surface keeps upload off day-one")
ok("confirm" in surf.lower() and "stay" in surf.lower(),
   "thin surface keeps confirm/apply off day-one")
ok("poll" in surf.lower(), "thin surface keeps mailbox poll off day-one")
ok("PM_INGEST_HTTP_PRIMARY" in surf or "dual-strip" in surf.lower(),
   "thin surface names dual-strip analogue")

production_unit = ROOT / "deploy/switchboard-ingest.service"
caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
live = "\n".join(
    line for line in caddy.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
if production_unit.is_file():
    ok("8126" in production_unit.read_text(encoding="utf-8"),
       "later authorized cut keeps the production unit on :8126")
    ok("8126" in live, "later authorized cut routes live Caddy to Ingest :8126")
else:
    ok(True, "no production Ingest systemd unit yet (charter only)")
    ok("8126" not in live, "live Caddy does not yet route Ingest :8126")

tracker_text = tracker.read_text(encoding="utf-8") if tracker.is_file() else ""
ok("ARCH-MS-99" in tracker_text, "execution tracker lists ARCH-MS-99")
ok("0016" in tracker_text or "ADR-0016" in tracker_text, "tracker links ADR-0016")
ok("arch-ms-ingest-service" in tracker_text,
   "tracker lists arch-ms-ingest-service")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
