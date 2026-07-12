#!/usr/bin/env python3
"""ARCH-MS-21: the SPA composition root loads explicit frontend boundaries."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(bool(condition))
    failed += int(not condition)


modules = ("api", "state", "board", "closure", "mission")
positions = [INDEX.find(f'src="js/{name}.js?v=') for name in modules]
app_position = INDEX.find('src="app.js?v=')
ok(all(pos >= 0 for pos in positions), "index loads all frontend modules")
ok(positions == sorted(positions) and positions[-1] < app_position,
   "frontend modules load in dependency order before app.js")

for name in modules:
    path = STATIC / "js" / f"{name}.js"
    source = path.read_text(encoding="utf-8")
    ok(path.is_file() and len(source.splitlines()) > 10, f"{name}.js is a substantive boundary")
    ok(f"Switchboard{name.title()}" in source, f"{name}.js publishes its explicit namespace")

for namespace in ("SwitchboardState", "SwitchboardBoard", "SwitchboardClosure", "SwitchboardMission"):
    ok(f"window.{namespace}" in APP, f"app.js composes {namespace}")
ok(len(APP.splitlines()) < 5_000, "app.js composition root stays below 5,000 lines")
ok("    _missionDeliverableFromUrl() {" not in APP and "    renderBoard() {" not in APP,
   "mission and board implementations no longer live in app.js")

print(f"\nARCH-MS-21 frontend modules: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
