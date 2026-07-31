#!/usr/bin/env python3
"""The Fleet host row shows whether a host can actually do the work.

A heartbeat badge only ever said "live". On 2026-07-31 a live, green host ran a
bundle whose execution-assignment contract this server refuses at admission, and
three Wave A missions died on it before anyone could see why. Liveness and
readiness are separate lights, and this asserts the second one renders — in a
browser, against the real render path, not by reading the source.

What is pinned:
  - each readiness state produces a distinguishable dot colour and label
  - a version delta is shown as installed → required, so the skew is legible
  - the update control appears only when there is something to install
  - a blocked host makes that control the loud one
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from path_setup import ROOT  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SCRIPTS = [
    "static/js/api.js", "static/js/state.js", "static/js/board.js",
    "static/js/plan-chat.js", "static/js/mission.js", "static/js/runner-session.js",
    "static/js/proof-console.js", "static/js/project-admin.js", "static/js/settings.js",
    "static/js/scope.js", "static/js/attention.js", "static/js/fleet-dock.js",
    "static/app.js",
]


def host(readiness):
    return {"host_id": "host/mac-1", "hostname": "steve-mac", "stale": False,
            "heartbeat_at": 0, "capacity": {"active_sessions": 1},
            "limits": {"max_sessions": 8},
            "runtimes": [{"runtime": "codex"}], "readiness": readiness}


READY = {"state": "ready", "installed_version": "0.4.16",
         "required_version": "0.4.16", "actionable": False,
         "detail": "Running the promoted release 0.4.16."}
BLOCKED = {"state": "blocked", "installed_version": "0.4.15",
           "required_version": "0.4.16", "actionable": True,
           "reason": "host_release_incompatible",
           "detail": "Bundled execution-assignment contract eac1:old cannot "
                     "satisfy the server's eac1:new. Every launch would be "
                     "refused at admission."}
BEHIND = {"state": "update_available", "installed_version": "0.4.15",
          "required_version": "0.4.16", "actionable": True,
          "detail": "Installed 0.4.15; 0.4.16 is promoted."}
UPDATING = {"state": "updating", "installed_version": "0.4.15",
            "required_version": "0.4.16", "actionable": False,
            "detail": "Host is draining; it stops taking work until its live "
                      "runners finish."}

failures = []


def ok(condition, message):
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


RENDER = """(h) => {
    const ctx = Object.create(TeepPlan);
    ctx.isAdmin = true;
    ctx.project = 'switchboard';
    const html = ctx._hostRow(h);
    const wrap = document.createElement('table');
    wrap.innerHTML = '<tbody>' + html + '</tbody>';
    document.body.innerHTML = '';
    document.body.appendChild(wrap);
    const dot = wrap.querySelector('.status-dot');
    const dotStyle = dot ? getComputedStyle(dot) : null;
    const dotBox = dot ? dot.getBoundingClientRect() : null;
    const update = wrap.querySelector('[data-host-update]');
    const box = update ? update.getBoundingClientRect() : null;
    return {
        text: wrap.innerText.replace(/\\s+/g, ' ').trim(),
        dotClass: dot ? dot.className : '',
        dotTitle: dot ? dot.getAttribute('title') : '',
        dotColor: dotStyle ? dotStyle.backgroundColor : '',
        dotSize: dotBox ? Math.round(dotBox.width * 10) / 10 : 0,
        hasUpdate: !!update,
        updateLabel: update ? update.textContent.trim() : '',
        updateClass: update ? update.className : '',
        updateWidth: box ? Math.round(box.width) : 0,
        updateHeight: box ? Math.round(box.height) : 0,
    };
}"""

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_content('<body style="margin:0"><div id="fleet-dock"></div></body>')
    # Tabler paints .status-dot and the bg-* colours. Without it the dots render
    # as invisible zero-colour spans — which is precisely the failure mode this
    # test exists to catch, so the real stylesheet is not optional here.
    page.add_style_tag(path=str(ROOT / "static" / "vendor" / "tabler" / "css" / "tabler.min.css"))
    page.add_style_tag(path=str(ROOT / "static" / "taikun-ui.css"))
    for rel in SCRIPTS:
        page.add_script_tag(path=str(ROOT / rel))

    ready = page.evaluate(RENDER, host(READY))
    ok("green" in ready["dotClass"], f"a current host is green: {ready['dotClass']}")
    ok("ready" in ready["text"], f"and says so: {ready['text'][:80]}")
    ok(ready["hasUpdate"] is False,
       "a current host offers no update button — there is nothing to install")

    blocked = page.evaluate(RENDER, host(BLOCKED))
    ok("bg-red" in blocked["dotClass"],
       f"an incompatible host is RED, not merely stale-yellow: {blocked['dotClass']}")
    ok("incompatible" in blocked["text"],
       f"the word says work is impossible, not just old: {blocked['text'][:90]}")
    ok("0.4.15" in blocked["text"] and "0.4.16" in blocked["text"],
       f"the skew is legible as installed → required: {blocked['text'][:90]}")
    ok("refused at admission" in (blocked["dotTitle"] or ""),
       "hovering explains why, in the server's own words")
    ok(blocked["hasUpdate"] and blocked["updateLabel"] == "Update host",
       f"a red host offers the fix: {blocked['updateLabel']!r}")
    ok("btn-danger" in blocked["updateClass"],
       f"and it is the loud button: {blocked['updateClass']}")

    behind = page.evaluate(RENDER, host(BEHIND))
    ok("yellow" in behind["dotClass"],
       f"a behind-but-working host is amber, not red: {behind['dotClass']}")
    ok(behind["hasUpdate"] and "btn-danger" not in behind["updateClass"],
       "it offers the update quietly — this host can still work")

    updating = page.evaluate(RENDER, host(UPDATING))
    ok("blue" in updating["dotClass"],
       f"a self-updating host is distinct from both: {updating['dotClass']}")
    ok(updating["hasUpdate"] is False,
       "no button while it is already updating itself")

    # The button must match the size the rest of the dock settled on. An action
    # that renders as a bare sliver has shipped here three times.
    ok(blocked["updateWidth"] > 60,
       f"the update button has real width: {blocked['updateWidth']}px")
    ok(blocked["updateHeight"] > 16,
       f"and real height: {blocked['updateHeight']}px")

    # An old server that does not send readiness must not blank the column or
    # throw: hosts predate this field and still have to render.
    legacy = page.evaluate(RENDER, {"host_id": "host/old", "hostname": "old",
                                    "stale": False, "heartbeat_at": 0,
                                    "capacity": {}, "limits": {},
                                    "runtimes": [{"runtime": "codex"}]})
    ok("green" in legacy["dotClass"] and legacy["hasUpdate"] is False,
       f"a host with no readiness field renders as ready: {legacy['dotClass']}")

    stale_legacy = page.evaluate(RENDER, {"host_id": "host/old", "hostname": "old",
                                          "stale": True, "heartbeat_at": 0,
                                          "capacity": {}, "limits": {},
                                          "runtimes": [{"runtime": "codex"}]})
    ok("offline" in stale_legacy["text"],
       f"and a stale one reads offline: {stale_legacy['text'][:60]}")

    # The dot must actually be painted, and each state must be a DIFFERENT
    # colour. Asserting class names alone would pass even if Tabler stopped
    # shipping bg-red — an invisible light is worse than none, because the
    # operator reads "no warning" as "nothing wrong".
    swatches = {"ready": ready["dotColor"], "blocked": blocked["dotColor"],
                "update_available": behind["dotColor"], "updating": updating["dotColor"]}
    for name, colour in swatches.items():
        ok(colour not in ("", "rgba(0, 0, 0, 0)", "transparent"),
           f"the {name} dot is actually painted: {colour}")
    ok(len(set(swatches.values())) == 4,
       f"all four states are visually distinct: {swatches}")
    ok(blocked["dotSize"] >= 5,
       f"the dot is big enough to see: {blocked['dotSize']}px")

    ok(not errors, f"no page errors: {errors}")
    browser.close()

print(f"\nHost readiness light: {len(failures)} failed")
if failures:
    raise SystemExit(1)
