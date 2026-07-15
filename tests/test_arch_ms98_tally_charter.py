#!/usr/bin/env python3
"""ARCH-MS-98: Tally/economics ADR charter + Mode A thin surface lock."""
from __future__ import annotations

from path_setup import ROOT

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


adr = ROOT / "docs/decisions/0015-tally-economics-process-strangler.md"
surface = ROOT / "docs/tally/thin_day_one_surface.md"
tracker = ROOT / "docs/ARCH-MS-EXECUTION.md"

ok(adr.is_file(), "ADR-0015 present")
adr_text = adr.read_text(encoding="utf-8") if adr.is_file() else ""
ok("ADR-0015" in adr_text or "0015" in adr_text, "ADR titled 0015")
ok("ARCH-MS-98" in adr_text, "ADR cites ARCH-MS-98")
ok("arch-ms-tally-service" in adr_text,
   "ADR is plan-of-record for arch-ms-tally-service")
ok("8125" in adr_text, "ADR locks port :8125")
ok("Mode A" in adr_text or "thin day-one" in adr_text.lower(), "ADR Mode A / thin surface")
ok("yellow" in adr_text.lower() or "CONDITIONAL" in adr_text, "ADR yellow-light / conditional cut")
ok("No-Go" in adr_text, "ADR documents No-Go exit")
ok("ADR-0014" in adr_text and "ADR-0012" in adr_text, "ADR reuses Deliverables/Tasks lineage")
ok("network coupling" in adr_text.lower() or "Never convert" in adr_text,
   "ADR forbids network-wrap without independence")
ok("MCP" in adr_text and "8111" in adr_text, "ADR keeps MCP on monolith/:8111")
ok("ledger" in adr_text.lower() or "kpis" in adr_text.lower(),
   "ADR scopes ledger / KPI read surface")

ok(surface.is_file(), "thin_day_one_surface.md present")
surf = surface.read_text(encoding="utf-8") if surface.is_file() else ""
ok(":8125" in surf or "8125" in surf, "thin surface names :8125")
for route in (
    "/tally/v1/kpis",
    "/tally/v1/outcomes",
    "/tally/v1/project",
    "/tally/v1/task/{task_id}",
    "/tally/v1/kpi/{kpi_id}",
    "/tally/v1/deliverable/{deliverable_id}",
):
    ok(route in surf, f"thin surface lists {route}")
ok("spend/ingest" in surf and "stay" in surf.lower(),
   "thin surface keeps spend ingest off day-one")
ok("verify" in surf.lower() or "reject" in surf.lower(),
   "thin surface keeps outcome verify/reject off day-one")
ok("PM_TALLY_HTTP_PRIMARY" in surf or "dual-strip" in surf.lower(),
   "thin surface names dual-strip analogue")

ok(not (ROOT / "deploy/switchboard-tally.service").is_file(),
   "no production Tally systemd unit yet (charter only)")
caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
live = "\n".join(
    line for line in caddy.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
ok("8125" not in live, "live Caddy does not yet route Tally :8125")

tracker_text = tracker.read_text(encoding="utf-8") if tracker.is_file() else ""
ok("ARCH-MS-98" in tracker_text, "execution tracker lists ARCH-MS-98")
ok("0015" in tracker_text or "ADR-0015" in tracker_text, "tracker links ADR-0015")
ok("arch-ms-tally-service" in tracker_text,
   "tracker lists arch-ms-tally-service")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
